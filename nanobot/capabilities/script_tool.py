"""Script tools: capabilities an author writes without changing kernel code (spec 012 Part C).

A bundle ``<workspace>/tools-script/<name>/`` holds ``tool.yaml``::

    name: <name>                  # == directory name, skill naming rule
    description: <1-1024 chars>
    parameters: {JSON Schema object}
    output_schema: {JSON Schema}  # optional
    io: {inputs, outputs, effects}  # spec 011 block, required
    entry: run                    # relative path to an executable inside the bundle
    timeout_s: 30                 # 1-600

:func:`load_manifest` validates it (:class:`ManifestError` naming the field).
:class:`ScriptTool` runs ``entry`` per call with:

- cwd = the bundle, stdin = the canonical JSON of the arguments;
- an environment of only ``PATH=/usr/bin:/bin``, ``HOME=<fresh temp dir>``,
  ``LANG=C.UTF-8`` and, when ``io.effects`` reads or writes the workspace,
  ``MOEKA_WORKSPACE``;
- the whole process group killed after ``timeout_s`` (error result ``timeout``);
- stdout parsed as one JSON value (at most 1 MiB), checked against ``output_schema``;
- under bubblewrap with ``--unshare-net`` when ``io.effects`` lacks ``network`` and
  bwrap works on this host; otherwise unsandboxed, recorded as ``sandboxed: false`` in
  the result metadata and the ``script_tool.call`` trace event.

Kernel code: imports no host package.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, capability_request
from nanobot.capabilities.manifest import IOSpec, ManifestError, parse_io
from nanobot.capabilities.provenance import canonical_json

if TYPE_CHECKING:
    from nanobot.capabilities.enforce import CertGate
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.policy import CapabilityRequest

MANIFEST_FILENAME = "tool.yaml"
SCRIPT_TOOLS_DIRNAME = "tools-script"
TOOL_PREFIX = "script_"
MAX_STDOUT_BYTES = 1 << 20  # 1 MiB
STDERR_TAIL_CHARS = 2000
DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 600
SCRIPT_PATH = "/usr/bin:/bin"
_WORKSPACE_EFFECTS = frozenset({"read-workspace", "write-workspace"})
_KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class ScriptManifest:
    """A validated ``tool.yaml``.

    ``entry`` is the manifest's relative path; ``entry_path`` the absolute executable
    inside ``bundle_dir``. ``io`` is the normalised spec 011 block and ``raw_io`` the
    block as written.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    io: IOSpec
    entry: str
    timeout_s: int
    bundle_dir: Path
    output_schema: dict[str, Any] | None = None
    raw_io: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def entry_path(self) -> Path:
        return self.bundle_dir / self.entry

    @property
    def tool_name(self) -> str:
        return TOOL_PREFIX + self.name

    @property
    def effects(self) -> tuple[str, ...]:
        return self.io.effects


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def load_manifest(bundle_dir: str | Path) -> ScriptManifest:
    """Read and validate ``<bundle_dir>/tool.yaml`` (spec 012 FR-009).

    Raises :class:`ManifestError` whose ``field`` names the bad field: ``tool.yaml``
    (missing, unreadable or not a mapping), ``name``, ``description``, ``parameters``,
    ``output_schema``, ``io`` (or ``io.<sub>`` from ``parse_io``), ``entry`` (missing,
    escapes the bundle, a symlink, not a regular file, not executable) or
    ``timeout_s``.
    """
    from nanobot.agent.skills import _SKILL_NAME

    bundle = Path(os.path.abspath(Path(bundle_dir)))
    manifest_file = bundle / MANIFEST_FILENAME
    try:
        text = manifest_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError(MANIFEST_FILENAME, f"not found in {bundle}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(MANIFEST_FILENAME, f"cannot be read ({exc})") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(MANIFEST_FILENAME, f"is not valid YAML ({exc})") from None
    if not isinstance(raw, dict):
        raise ManifestError(MANIFEST_FILENAME, "must be a YAML mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("name", "required string")
    if name != bundle.name:
        raise ManifestError("name", f"{name!r} does not match the directory name {bundle.name!r}")
    if len(name) > 64 or _SKILL_NAME.fullmatch(name) is None:
        raise ManifestError("name", f"{name!r} must be hyphen-case (a-z, 0-9), at most 64 chars")

    description = raw.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise ManifestError("description", "must be a string of 1-1024 characters")

    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        raise ManifestError("parameters", "required JSON Schema object (a mapping)")
    if parameters.get("type", "object") != "object":
        raise ManifestError("parameters", "must be a JSON Schema of type 'object'")
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        raise ManifestError("parameters", "'properties' must be a mapping")
    parameters = {"type": "object", **parameters}

    output_schema = raw.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ManifestError("output_schema", "must be a JSON Schema (a mapping)")

    if raw.get("io") is None:
        raise ManifestError("io", "required (spec 011 io block)")
    io = parse_io(raw["io"])
    assert io is not None

    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        isinstance(timeout, bool) or not isinstance(timeout, int)
        or not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S
    ):
        raise ManifestError(
            "timeout_s", f"must be an integer from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}",
        )

    entry = raw.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        raise ManifestError("entry", "required relative path to an executable")
    _check_entry(bundle, entry)

    return ScriptManifest(
        name=name,
        description=description,
        parameters=parameters,
        io=io,
        entry=entry,
        timeout_s=timeout,
        bundle_dir=bundle,
        output_schema=output_schema,
        raw_io=dict(raw["io"]) if isinstance(raw["io"], dict) else {},
    )


def _check_entry(bundle: Path, entry: str) -> None:
    if Path(entry).is_absolute():
        raise ManifestError("entry", f"{entry!r} must be relative to the bundle")
    lexical = Path(os.path.normpath(bundle / entry))
    if not _is_within(lexical, bundle) or lexical == bundle:
        raise ManifestError("entry", f"{entry!r} escapes the bundle")
    # Every component below the bundle must be a real directory/file, never a link.
    current = bundle
    for part in lexical.relative_to(bundle).parts:
        current = current / part
        if current.is_symlink():
            raise ManifestError("entry", f"{entry!r} is (or passes through) a symlink")
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ManifestError("entry", f"{entry!r} does not exist") from None
    if not _is_within(resolved, bundle.resolve()):
        raise ManifestError("entry", f"{entry!r} escapes the bundle")
    try:
        mode = lexical.lstat().st_mode
    except OSError:
        raise ManifestError("entry", f"{entry!r} does not exist") from None
    if not stat.S_ISREG(mode):
        raise ManifestError("entry", f"{entry!r} is not a regular file")
    if not os.access(lexical, os.X_OK):
        raise ManifestError("entry", f"{entry!r} is not executable")


# -- sandbox --------------------------------------------------------------------------


@functools.cache
def bwrap_available() -> bool:
    """Whether bubblewrap can start a network-less sandbox on this host (probed once).

    ``bwrap`` on PATH is not enough: containers and userns-restricted hosts ship a
    binary that cannot unshare the network, so a tiny sandbox is actually started.
    """
    binary = shutil.which("bwrap")
    if binary is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--unshare-net", "--unshare-pid", "--die-with-parent", "--", "/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _bwrap_argv(
    manifest: ScriptManifest, home: Path, workspace: Path | None,
) -> list[str]:
    binary = shutil.which("bwrap") or "bwrap"
    bundle = str(manifest.bundle_dir)
    argv = [
        binary, "--new-session", "--die-with-parent", "--unshare-net", "--unshare-pid",
        "--ro-bind", "/usr", "/usr",
    ]
    for optional in ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/etc/alternatives",
                     "/etc/ld.so.cache", "/etc/ssl/certs", "/etc/localtime"):
        argv += ["--ro-bind-try", optional, optional]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    if workspace is not None:
        effects = set(manifest.io.effects)
        flag = "--bind" if "write-workspace" in effects else "--ro-bind"
        argv += [flag, str(workspace), str(workspace)]
    argv += ["--bind", str(home), str(home), "--ro-bind", bundle, bundle]
    argv += ["--chdir", bundle, "--", str(manifest.entry_path)]
    return argv


# -- the tool -------------------------------------------------------------------------


def _tail(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ScriptTool(Tool):
    """One script-tool bundle as the tool ``script_<name>`` (spec 012 FR-010).

    *manifest* is a :class:`ScriptManifest` or a bundle directory (loaded with
    :func:`load_manifest`). *workspace* is exported as ``MOEKA_WORKSPACE`` (and
    bind-mounted) only when ``io.effects`` reads or writes the workspace. *sandbox*
    ``None`` = bubblewrap when the manifest lacks ``network`` and bwrap works; ``False``
    forces no sandbox. ``script_tool.call`` trace events go to *trace* (else
    ``env.trace``).
    """

    _plugin_discoverable = False
    _capability_names = frozenset({"exec.run"})

    def __init__(
        self,
        manifest: ScriptManifest | str | Path,
        *,
        workspace: str | Path | None = None,
        env: CoreEnvironment | None = None,
        trace: Any = None,
        sandbox: bool | None = None,
        cert_id: str | None = None,
        cert_status: str = "uncertified",
    ) -> None:
        if not isinstance(manifest, ScriptManifest):
            manifest = load_manifest(manifest)
        self.manifest = manifest
        self._workspace = Path(workspace) if workspace is not None else None
        self._env = env
        self._trace = trace if trace is not None else (env.trace if env is not None else None)
        self._sandbox = sandbox
        self.cert_id = cert_id
        self.cert_status = cert_status
        self.io = dict(manifest.raw_io) or manifest.io.to_dict()
        self.output_schema = manifest.output_schema
        self.last_metadata: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.manifest.tool_name

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def parameters(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.manifest.parameters))

    @property
    def sandboxed(self) -> bool:
        """Whether calls run under bubblewrap (no ``network`` effect and bwrap works)."""
        if self._sandbox is False or "network" in self.manifest.io.effects:
            return False
        return bwrap_available()

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request("exec.run", str(self.manifest.entry_path))]

    def _child_env(self, home: Path) -> dict[str, str]:
        env = {"PATH": SCRIPT_PATH, "HOME": str(home), "LANG": "C.UTF-8"}
        if self._workspace is not None and _WORKSPACE_EFFECTS & set(self.manifest.io.effects):
            env["MOEKA_WORKSPACE"] = str(self._workspace)
        return env

    def _emit(self, *, sandboxed: bool, exit_code: int | None, duration_ms: int) -> None:
        if self._trace is None:
            return
        from nanobot.kernel.trace import safe_emit

        safe_emit(self._trace, {
            "event": "script_tool.call",
            "tool": self.name,
            "effects_declared": list(self.manifest.io.effects),
            "sandboxed": sandboxed,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
        })

    def _result(
        self, text: str, *, error: bool, sandboxed: bool, exit_code: int | None,
        duration_ms: int, structured: Any = None, has_structured: bool = False,
    ) -> ToolResult:
        metadata = {
            "tool": self.name,
            "sandboxed": sandboxed,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "effects_declared": list(self.manifest.io.effects),
        }
        if has_structured:
            result = ToolResult(text, is_error=error, structured=structured, metadata=metadata)
        else:
            result = ToolResult(text, is_error=error, metadata=metadata)
        self.last_metadata = metadata
        return result

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = self.name
        errors = self.validate_params(kwargs)
        if errors:
            return ToolResult.error(
                f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            )
        sandboxed = self.sandboxed
        home = Path(tempfile.mkdtemp(prefix="moeka-script-home-"))
        workspace = (
            self._workspace
            if self._workspace is not None and _WORKSPACE_EFFECTS & set(self.manifest.io.effects)
            else None
        )
        argv = (
            _bwrap_argv(self.manifest, home, workspace) if sandboxed
            else [str(self.manifest.entry_path)]
        )
        started = time.monotonic()
        exit_code: int | None = None
        try:
            outcome = await self._run(argv, home, canonical_json(kwargs))
        except OSError as exc:
            duration = int((time.monotonic() - started) * 1000)
            self._emit(sandboxed=sandboxed, exit_code=None, duration_ms=duration)
            return self._result(
                f"Error: script tool '{name}' could not start: {exc}", error=True,
                sandboxed=sandboxed, exit_code=None, duration_ms=duration,
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
        status, exit_code, stdout, stderr = outcome
        duration = int((time.monotonic() - started) * 1000)
        self._emit(sandboxed=sandboxed, exit_code=exit_code, duration_ms=duration)

        def fail(text: str) -> ToolResult:
            return self._result(text, error=True, sandboxed=sandboxed, exit_code=exit_code,
                                duration_ms=duration)

        if status == "timeout":
            return fail(
                f"Error: script tool '{name}' timeout: killed after {self.manifest.timeout_s}s"
            )
        if status == "overflow":
            return fail(
                f"Error: script tool '{name}' wrote more than {MAX_STDOUT_BYTES} bytes to stdout"
            )
        if exit_code != 0:
            return fail(
                f"Error: script tool '{name}' exited with code {exit_code}\n"
                f"stderr (last {STDERR_TAIL_CHARS} chars):\n{_tail(stderr)}"
            )
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return fail(
                f"Error: script tool '{name}' stdout is not one JSON value (exit code "
                f"{exit_code})\nstderr (last {STDERR_TAIL_CHARS} chars):\n{_tail(stderr)}"
            )
        result = self._result(
            json.dumps(value, ensure_ascii=False), error=False, sandboxed=sandboxed,
            exit_code=exit_code, duration_ms=duration, structured=value, has_structured=True,
        )
        # The existing typed-call check (the registry runs it after every call too):
        # same validator, same RESULT_SCHEMA_MARKER error shaping, same trace event.
        from nanobot.kernel.typed import validate_result

        checked = validate_result(self, result, env=self._env)
        if checked is not result:
            checked.metadata = dict(result.metadata)
        return checked

    async def _run(
        self, argv: list[str], home: Path, stdin: bytes,
    ) -> tuple[str, int | None, bytes, bytes]:
        """Run *argv*; returns ``(status, exit code, stdout, stderr)``.

        ``status`` is ``ok``, ``timeout`` (whole group killed after ``timeout_s``) or
        ``overflow`` (stdout above 1 MiB; group killed).
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.manifest.bundle_dir),
            env=self._child_env(home),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout = bytearray()
        stderr = bytearray()
        overflow = asyncio.Event()

        async def feed() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001 - already closed by the child exiting
                    pass

        async def read_out() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(1 << 16):
                stdout.extend(chunk)
                if len(stdout) > MAX_STDOUT_BYTES:
                    overflow.set()
                    _kill_group(proc)
                    return

        async def read_err() -> None:
            assert proc.stderr is not None
            keep = 8 * STDERR_TAIL_CHARS  # bytes: enough for the last 2000 characters
            while chunk := await proc.stderr.read(1 << 16):
                stderr.extend(chunk)
                if len(stderr) > 2 * keep:
                    del stderr[: len(stderr) - keep]

        async def run() -> int:
            readers = asyncio.gather(read_out(), read_err())
            _, code = await asyncio.gather(feed(), proc.wait())
            # The entry exited: anything it left in its process group (a backgrounded
            # child holding stdout open) is killed so the readers see EOF.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await readers
            return code

        task = asyncio.ensure_future(run())
        try:
            code = await asyncio.wait_for(asyncio.shield(task), timeout=self.manifest.timeout_s)
        except asyncio.TimeoutError:
            _kill_group(proc)
            try:
                await asyncio.wait_for(task, timeout=_KILL_GRACE_S)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - best effort reap
                task.cancel()
            return "timeout", proc.returncode, bytes(stdout), bytes(stderr)
        except asyncio.CancelledError:
            _kill_group(proc)
            task.cancel()
            raise
        if overflow.is_set():
            return "overflow", code, bytes(stdout), bytes(stderr)
        return "ok", code, bytes(stdout), bytes(stderr)


# -- discovery ------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptBundle:
    """A workspace script-tool bundle: its manifest (or why it is invalid) and status."""

    name: str
    bundle_dir: Path
    manifest: ScriptManifest | None
    error: str | None
    cert_id: str | None
    cert_status: str
    allowed: bool


def scan_script_bundles(
    workspace: str | Path, gate: CertGate | None = None,
) -> list[ScriptBundle]:
    """Every ``<workspace>/tools-script/<name>/`` with a ``tool.yaml``, by name.

    An invalid manifest is reported (``error``) and never allowed. With *gate*, each
    valid bundle gets its certificate status (and ``allowed`` follows enforcement).
    """
    base = Path(workspace) / SCRIPT_TOOLS_DIRNAME
    if not base.is_dir():
        return []
    out: list[ScriptBundle] = []
    for bundle in sorted(base.iterdir()):
        if not bundle.is_dir() or not (bundle / MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = load_manifest(bundle)
        except ManifestError as exc:
            out.append(ScriptBundle(bundle.name, bundle, None, str(exc), None, "invalid", False))
            continue
        if gate is None:
            out.append(ScriptBundle(bundle.name, bundle, manifest, None, None, "uncertified",
                                    True))
            continue
        provenance = gate.check(bundle, kind="script tool", name=bundle.name)
        out.append(ScriptBundle(
            bundle.name, bundle, manifest, None, provenance.cert_id, provenance.status,
            provenance.allowed,
        ))
    return out


def build_script_tools(
    workspace: str | Path,
    gate: CertGate | None = None,
    *,
    env: CoreEnvironment | None = None,
    taken: Iterable[str] = (),
) -> list[ScriptTool]:
    """The loadable script tools of *workspace* (invalid manifests warned and skipped)."""
    taken_names = set(taken)
    tools: list[ScriptTool] = []
    for bundle in scan_script_bundles(workspace, gate):
        if bundle.manifest is None:
            logger.warning("Skipping script tool '{}': {}", bundle.name, bundle.error)
            continue
        if not bundle.allowed:
            continue
        tool = ScriptTool(
            bundle.manifest, workspace=workspace, env=env,
            cert_id=bundle.cert_id, cert_status=bundle.cert_status,
        )
        if tool.name in taken_names:
            logger.warning("Skipping script tool '{}': tool name already registered", tool.name)
            continue
        taken_names.add(tool.name)
        tools.append(tool)
    return tools


def settings_from(raw: Mapping[str, Any] | Any) -> bool:
    """``tools.script.enabled`` from a ``ToolsConfig`` (or a mapping)."""
    script = raw.get("script") if isinstance(raw, Mapping) else getattr(raw, "script", None)
    if isinstance(script, Mapping):
        return script.get("enabled") is True
    return getattr(script, "enabled", False) is True


__all__ = [
    "MANIFEST_FILENAME",
    "SCRIPT_TOOLS_DIRNAME",
    "ScriptBundle",
    "ScriptManifest",
    "ScriptTool",
    "bwrap_available",
    "build_script_tools",
    "load_manifest",
    "scan_script_bundles",
]
