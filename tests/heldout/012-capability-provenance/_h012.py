"""Helpers for the 012 held-out tests: lazy access to the feature modules, an independent
reference of the bundle hash / canonical JSON / cert id / key id, a FAKE Ed25519 fixture signer
(tests only; deterministic seeds), bundle and trust-dir builders, kernel / loader / registry
construction, and the CLI runner."""

from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

# -- lazy feature access ----------------------------------------------------------------------


def cap(module: str) -> Any:
    """``nanobot.capabilities.<module>`` imported at call time (a missing feature fails the
    calling test, not collection)."""
    return importlib.import_module(f"nanobot.capabilities.{module}")


def prov() -> Any:
    return cap("provenance")


def st() -> Any:
    return cap("script_tool")


def repo_root() -> Path:
    import importlib.util

    spec = importlib.util.find_spec("nanobot")
    assert spec is not None and spec.origin is not None
    return Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(spec.origin).resolve().parents[1])


# -- reference canonical JSON / hashes (spec "Terms") -------------------------------------------


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8")


def sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RefBundleError(Exception):
    pass


def ref_files(root: Path) -> list[tuple[str, str]]:
    """Every regular file except the top-level ``CERT.json`` as ``(posix relpath, sha256)``,
    sorted by relpath (Python string order); a symlink, fifo, socket or device raises."""
    out: list[tuple[str, str]] = []

    def walk(d: str, prefix: str) -> None:
        for name in os.listdir(d):
            path = os.path.join(d, name)
            rel = prefix + name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise RefBundleError(f"symlink {rel}")
            if stat.S_ISDIR(mode):
                walk(path, rel + "/")
            elif stat.S_ISREG(mode):
                if rel != "CERT.json":
                    with open(path, "rb") as fh:
                        out.append((rel, sha_hex(fh.read())))
            else:
                raise RefBundleError(f"special file {rel}")

    walk(str(root), "")
    return sorted(out)


def ref_bundle_sha256(root: Path) -> str:
    return sha_hex(canonical([[rel, digest] for rel, digest in ref_files(root)]))


def ref_cert_id(cert: Mapping[str, Any]) -> str:
    return sha_hex(canonical(cert))[:16]


def ref_key_id(pub_raw: bytes) -> str:
    return sha_hex(pub_raw)[:16]


def ref_io(raw: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Reference normalisation of a valid spec-011 io block, as the cert's list form."""
    raw = raw or {}
    inputs = raw.get("inputs")
    if inputs is None:
        all_, any_ = [], []
    elif isinstance(inputs, list):
        all_, any_ = list(inputs), []
    else:
        all_, any_ = list(inputs.get("all") or []), list(inputs.get("any") or [])
    outputs = raw["outputs"] if raw.get("outputs") is not None else ["any"]
    effects = raw["effects"] if raw.get("effects") is not None else ["exec"]
    return {"inputs_all": sorted(all_), "inputs_any": sorted(any_),
            "outputs": sorted(outputs), "effects": sorted(effects)}


# -- FAKE fixture signer (tests only) --------------------------------------------------------------


class Signer:
    """A deterministic fake Ed25519 key (seed = sha256("h012-fake-" + label))."""

    def __init__(self, label: str) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        seed = hashlib.sha256(f"h012-fake-{label}".encode()).digest()
        self.label = label
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        self.pub = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = ref_key_id(self.pub)
        self.pub_b64 = base64.b64encode(self.pub).decode("ascii")

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def sig_b64(self, cert: Mapping[str, Any]) -> str:
        return base64.b64encode(self.sign(canonical(cert))).decode("ascii")

    def write_pub(self, directory: Path, name: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name or self.label}.pub"
        path.write_text(self.pub_b64 + "\n", encoding="utf-8")
        return path


ISSUED_AT = "2026-10-03T12:00:00Z"
DELETE = object()


def bundle_kind(bundle: Path) -> str:
    return "skill" if (bundle / "SKILL.md").exists() else "script-tool"


def bundle_io(bundle: Path) -> dict[str, Any] | None:
    if (bundle / "SKILL.md").exists():
        text = (bundle / "SKILL.md").read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1])
        return front.get("io")
    return yaml.safe_load((bundle / "tool.yaml").read_text(encoding="utf-8")).get("io")


def make_cert(bundle: Path, signer: Signer, **over: Any) -> dict[str, Any]:
    """A valid cert for *bundle* as it is on disk now; ``over`` replaces top-level fields
    (``DELETE`` removes one)."""
    cert: dict[str, Any] = {
        "schema": "moeka.capcert.v1",
        "kind": bundle_kind(bundle),
        "name": bundle.name,
        "bundle_sha256": ref_bundle_sha256(bundle),
        "io": ref_io(bundle_io(bundle)),
        "scores": {"pass_rate": 1.0, "cases": 12},
        "lineage": {"parent": None},
        "verifier": {"version": "1.0.0", "suite": "h012-suite"},
        "issued_at": ISSUED_AT,
        "key_id": signer.key_id,
        "scope": "capabilities",
    }
    for key, value in over.items():
        if value is DELETE:
            cert.pop(key, None)
        else:
            cert[key] = value
    return cert


def write_cert(bundle: Path, cert: Mapping[str, Any], signer: Signer,
               sig: str | None = None) -> str:
    """Write ``CERT.json`` = ``{"cert", "sig"}``; returns the reference cert id."""
    doc = {"cert": dict(cert), "sig": sig if sig is not None else signer.sig_b64(cert)}
    (bundle / "CERT.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return ref_cert_id(cert)


def certify(bundle: Path, signer: Signer, **over: Any) -> str:
    """Sign *bundle* as it is now; returns the cert id."""
    return write_cert(bundle, make_cert(bundle, signer, **over), signer)


def read_cert(bundle: Path) -> dict[str, Any]:
    return json.loads((bundle / "CERT.json").read_text(encoding="utf-8"))["cert"]


def flip_byte(path: Path, needle: bytes = b"MARK") -> None:
    """Change one byte of *path* (case of the first byte of *needle*)."""
    data = bytearray(path.read_bytes())
    i = data.index(needle)
    data[i] ^= 0x20
    path.write_bytes(bytes(data))


# -- trust dirs ----------------------------------------------------------------------------------


def trust_dir(path: Path, *signers: Signer, revoked: Sequence[str] = (),
              extra_lines: Sequence[str] = ()) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for s in signers:
        s.write_pub(path)
    lines = [json.dumps({"cert_id": cid, "reason": "h012 test", "at": ISSUED_AT})
             for cid in revoked]
    lines += list(extra_lines)
    if lines:
        (path / "revoked.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def revoke(path: Path, cert_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / "revoked.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"cert_id": cert_id, "reason": "h012 revoke", "at": ISSUED_AT})
                 + "\n")


def trust_load(*dirs: Path) -> Any:
    return prov().TrustStore.load([Path(d) for d in dirs])


def verify(bundle: Path, trust: Any) -> Any:
    return prov().verify_bundle(bundle, trust)


# -- bundles on disk -----------------------------------------------------------------------------

DEFAULT_SKILL_IO = {"inputs": ["file:pdf"], "outputs": ["text/markdown"],
                    "effects": ["read-workspace"]}


def write_skill(ws: Path, name: str, description: str | None = None, *,
                io: Mapping[str, Any] | None = DEFAULT_SKILL_IO, body: str | None = None,
                extra_files: Mapping[str, str | bytes] | None = None) -> Path:
    """``<ws>/skills/<name>/SKILL.md`` (+ extra files); returns the bundle dir. The body
    carries ``BODY-MARK-<name>`` (and the letters ``MARK`` for :func:`flip_byte`)."""
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"name: {name}\n"
    front += f"description: {json.dumps(description or f'Digest PDF files for {name}')}\n"
    if io is not None:
        front += yaml.safe_dump({"io": json.loads(json.dumps(io))}, default_flow_style=None,
                                sort_keys=False)
    text = body if body is not None else f"BODY-MARK-{name} do the {name} task."
    (d / "SKILL.md").write_text(f"---\n{front}---\n\n{text}\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return d


PY = os.path.realpath(sys.executable)

SH_OK = '#!/bin/sh\n# MARK comment\ncat >/dev/null\necho \'{"ok": true}\'\n'


def py_script(body: str) -> str:
    return f"#!{PY}\n# MARK comment\n{body}"


DEFAULT_PARAMS = {"type": "object", "properties": {"q": {"type": "string"}}}
DEFAULT_TOOL_IO = {"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["none"]}
NET_IO = {"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["network"]}


def tool_manifest(name: str, /, **over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "name": name,
        "description": f"Script tool {name} for held-out tests",
        "parameters": DEFAULT_PARAMS,
        "io": DEFAULT_TOOL_IO,
        "entry": "run",
        "timeout_s": 30,
    }
    for key, value in over.items():
        if value is DELETE:
            m.pop(key, None)
        else:
            m[key] = value
    return m


def write_script_tool(ws: Path, name: str, /, *, script: str = SH_OK, mode: int = 0o755,
                      manifest: Mapping[str, Any] | None = None, raw_yaml: str | None = None,
                      entry_path: str = "run", **over: Any) -> Path:
    """``<ws>/tools-script/<name>/{tool.yaml, <entry_path>}``; returns the bundle dir."""
    d = ws / "tools-script" / name
    d.mkdir(parents=True, exist_ok=True)
    if raw_yaml is not None:
        (d / "tool.yaml").write_text(raw_yaml, encoding="utf-8")
    else:
        m = dict(manifest) if manifest is not None else tool_manifest(name, **over)
        (d / "tool.yaml").write_text(yaml.safe_dump(json.loads(json.dumps(m)), sort_keys=False),
                                     encoding="utf-8")
    if script is not None and entry_path:
        p = d / entry_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(script, encoding="utf-8")
        p.chmod(mode)
    return d


# -- config / kernels / loaders ------------------------------------------------------------------


def config(caps: Mapping[str, Any] | None = None, script_enabled: bool | None = None) -> Any:
    from nanobot.config.schema import Config

    kwargs: dict[str, Any] = {}
    if caps is not None:
        kwargs["capabilities"] = dict(caps)
    if script_enabled is not None:
        kwargs["tools"] = {"script": {"enabled": script_enabled}}
    return Config(**kwargs)


def caps_for(enforce: bool, *dirs: Path, **extra: Any) -> dict[str, Any]:
    return {"enforce_certs": enforce, "trust_dirs": [str(d) for d in dirs], **extra}


def make_kernel(state: Path, *, caps: Mapping[str, Any] | None, script_enabled: bool | None,
                builtin: Path | None, work: Path, trace: Any, fake: Any = None) -> tuple[Any, Any]:
    from moeka import Environment, Kernel, ModelSpec
    from moeka.testing import FakeProvider
    from moeka.variants import Variant

    work.mkdir(parents=True, exist_ok=True)
    env = Environment.from_config(config(caps, script_enabled), state_dir=state, work_dir=work,
                                  trace=trace)
    kernel = Kernel(env, variant=Variant(builtin_skills_dir=builtin)) if builtin else Kernel(env)
    fk = fake if fake is not None else FakeProvider(default="ok")
    kernel.llm.register_provider(
        "default", fk, ModelSpec(name="default", model="fake-main", provider="openai"),
    )
    return kernel, fk


def agent_of(kernel: Any, name: str = "h012") -> Any:
    from moeka.agents import AgentSpec

    return kernel.agent(AgentSpec(name=name))


def tool_names(agent: Any) -> list[str]:
    return [t.name for t in agent.tools]


def tool_call(name: str, args: Mapping[str, Any], call_id: str = "c1") -> Any:
    from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=dict(args))],
        finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=10, output_tokens=5),
    )


def fake_with(*items: Any) -> Any:
    from moeka.testing import FakeProvider

    return FakeProvider(list(items), default="ok")


def tool_messages(fake: Any) -> list[str]:
    """Contents of every ``role == "tool"`` message the model saw on its LAST call."""
    msgs = fake.calls[-1].messages
    out = []
    for m in msgs:
        if m.get("role") == "tool":
            c = m.get("content")
            out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return out


def system_prompt_of(fake: Any, call: int = -1) -> str:
    msgs = fake.calls[call].messages
    assert msgs[0]["role"] == "system"
    return str(msgs[0]["content"])


def user_message_of(fake: Any, call: int = -1) -> str:
    return json.dumps(fake.calls[call].messages[-1].get("content"), ensure_ascii=False)


_SUMMARY_LINE = re.compile(r"^- \*\*(?P<name>[^*]+)\*\* — .*$", re.MULTILINE)


def summary_names(prompt: str) -> list[str]:
    return [m.group("name") for m in _SUMMARY_LINE.finditer(prompt)]


def ev(sink: Any, name: str) -> list[dict[str, Any]]:
    return [e for e in sink.events if e.get("event") == name]


STAMP_KEYS = {"event", "trace_id", "span", "tags", "ts"}


def strip(e: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in e.items() if k not in STAMP_KEYS}


def host_env(work: Path, state: Path, *, caps: Mapping[str, Any] | None = None,
             script_enabled: bool | None = None, trace: Any = None) -> Any:
    from moeka import Environment
    from moeka.trace import MemoryTraceSink

    work.mkdir(parents=True, exist_ok=True)
    return Environment.from_config(config(caps, script_enabled), state_dir=state, work_dir=work,
                                   trace=trace if trace is not None else MemoryTraceSink())


def skills_loader(work: Path, state: Path, *, caps: Mapping[str, Any] | None = None,
                  builtin: Path | None = None, inline: list | None = None) -> Any:
    """A ``SkillsLoader`` over *work* whose host env carries ``Config(capabilities=caps)``
    (``env.config.section("capabilities")``). When the constructor also takes one of the
    keywords below it is passed the matching value too (surface assumption, SPEC-MAP)."""
    from nanobot.agent.skills import SkillsLoader

    env = host_env(work, state, caps=caps)
    kwargs: dict[str, Any] = {"env": env.core}
    if builtin is not None:
        kwargs["builtin_skills_dir"] = builtin
    if inline is not None:
        kwargs["inline_skills"] = inline
    params = inspect.signature(SkillsLoader.__init__).parameters
    cfg = config(caps)
    c = dict(caps or {})
    if "capabilities" in params:
        kwargs["capabilities"] = cfg.capabilities
    if "capabilities_config" in params:
        kwargs["capabilities_config"] = cfg.capabilities
    if "enforce_certs" in params:
        kwargs["enforce_certs"] = bool(c.get("enforce_certs", False))
    if "trust_dirs" in params:
        kwargs["trust_dirs"] = list(c.get("trust_dirs", []))
    for key in ("trust", "trust_store"):
        if key in params and c.get("trust_dirs") is not None:
            kwargs[key] = trust_load(*[Path(d) for d in c["trust_dirs"]])
    return SkillsLoader(work, **kwargs)


def listed(loader: Any) -> list[str]:
    return [s["name"] for s in loader.list_skills(filter_unavailable=False)]


def loader_registry(work: Path, state: Path, *, caps: Mapping[str, Any] | None = None,
                    script_enabled: bool | None = True, trace: Any = None) -> Any:
    """Built-in tools (+ script tools) loaded by ``ToolLoader().load`` with a ``ToolContext``
    whose ``config`` is ``Config.tools`` and whose ``env`` is the host env's core env."""
    from loguru import logger

    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.agent.tools.registry import ToolRegistry

    env = host_env(work, state, caps=caps, script_enabled=script_enabled, trace=trace)
    cfg = config(caps, script_enabled)
    registry = ToolRegistry()
    logger.disable("nanobot.agent.tools.loader")
    try:
        ToolLoader().load(ToolContext(config=cfg.tools, workspace=str(work), env=env.core),
                          registry)
    finally:
        logger.enable("nanobot.agent.tools.loader")
    return registry


# -- sandbox probe -------------------------------------------------------------------------------


def bwrap_state() -> tuple[str, str]:
    """``("absent"|"usable"|"broken", detail)``: is ``bwrap --unshare-net`` usable here?"""
    path = shutil.which("bwrap")
    if path is None:
        return "absent", "bwrap not on PATH"
    try:
        proc = subprocess.run(
            [path, "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "/bin/true"], capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return "broken", f"bwrap probe failed: {exc}"
    if proc.returncode != 0:
        return "broken", f"bwrap present but unusable here: {proc.stderr.strip()[:200]}"
    return "usable", path


# -- CLI -------------------------------------------------------------------------------------------


def cli(tmp_path: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    home = tmp_path / "clihome"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("MOEKA_WORKSPACE", "MOEKA_CONFIG", "MOEKA_STATE", "NANOBOT_HOME")}
    env.update(HOME=str(home), PYTHONPATH=str(repo_root()), PYTHONDONTWRITEBYTECODE="1",
               NO_COLOR="1", COLUMNS="250", TERM="dumb")
    return subprocess.run([sys.executable, "-m", "nanobot", "capabilities", *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=timeout)


def write_config(path: Path, *, caps: Mapping[str, Any] | None = None,
                 script_enabled: bool | None = None, workspace: Path | None = None) -> Path:
    doc: dict[str, Any] = {}
    if caps is not None:
        doc["capabilities"] = dict(caps)
    if script_enabled is not None:
        doc["tools"] = {"script": {"enabled": script_enabled}}
    if workspace is not None:
        doc["agents"] = {"defaults": {"workspace": str(workspace)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def line_for(stdout: str, *names: str) -> str | None:
    """The first output line having one of *names* as a whitespace-separated token."""
    for line in stdout.splitlines():
        toks = line.split()
        if any(n in toks for n in names):
            return line
    return None


def has_token(line: str, token: str) -> bool:
    return token in line.split()


VERIFY_CODES = (
    "no-cert", "bad-json", "bad-schema", "unknown-key", "bad-signature", "name-mismatch",
    "kind-mismatch", "scope-mismatch", "hash-mismatch", "io-mismatch", "revoked", "bad-bundle",
)
