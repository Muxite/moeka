"""A thin client for the ``awr`` command line (awork-resume), driven only via ``--json``.

awork-resume stays independent of moeka: nothing here imports it. Every call runs
``<awr> <command> ... --workspace W --json`` as a subprocess and reads the one JSON
envelope it prints (``schema_version``, ``command``, ``status``, ``exit_code``,
``error``, ``data``; see awork-resume ``AGENTS.md``).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_NEEDS_USER = 4


class AwrError(RuntimeError):
    """``awr`` could not be run, printed no envelope, or failed (exit 1, 2 or 3)."""

    def __init__(self, message: str, envelope: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.envelope = dict(envelope or {})

    @property
    def exit_code(self) -> int | None:
        code = self.envelope.get("exit_code")
        return code if isinstance(code, int) else None


def resolve_command(awr: str | None, project: str | Path | None) -> list[str]:
    """The argv prefix for ``awr``: an explicit command, ``uv run --project``, or PATH."""
    if awr:
        return shlex.split(awr)
    if project:
        return ["uv", "run", "--quiet", "--project", str(project), "awr"]
    return ["awr"]


class AwrClient:
    """Runs ``awr`` against one workspace; the posting is fixed by the host."""

    def __init__(
        self,
        command: Sequence[str],
        workspace: Path,
        *,
        ollama_url: str | None = None,
        timeout_s: float = 3 * 3600,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.command = list(command)
        self.workspace = Path(workspace)
        self.timeout_s = timeout_s
        env = dict(os.environ)
        if ollama_url:
            env["AWR_OLLAMA_URL"] = ollama_url
        env.update(extra_env or {})
        self._env = env

    def run(self, *args: str) -> dict[str, Any]:
        """Run one command; return its envelope (exit 0 or 4), else raise :class:`AwrError`."""
        argv = [*self.command, *args, "--workspace", str(self.workspace), "--json"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, env=self._env, timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AwrError(f"awr not found: {argv[0]} ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise AwrError(f"awr {args[0] if args else ''} timed out after {self.timeout_s}s") from exc
        envelope = _parse_envelope(proc.stdout)
        if envelope is None:
            tail = (proc.stderr or "").strip().splitlines()[-5:]
            raise AwrError(
                f"awr {' '.join(args[:1])} printed no JSON envelope (exit {proc.returncode}): "
                + " | ".join(tail)
            )
        if envelope.get("exit_code") not in (EXIT_OK, EXIT_NEEDS_USER):
            err = envelope.get("error") or {}
            raise AwrError(
                f"awr {envelope.get('command')} failed (exit {envelope.get('exit_code')}, "
                f"{err.get('code')}): {err.get('message')}; hint: {envelope.get('hint')}",
                envelope,
            )
        return envelope

    def build(self, posting: Path) -> dict[str, Any]:
        return self.run("build", "--posting", str(posting))

    def questions(self) -> dict[str, Any]:
        return self.run("questions")

    @staticmethod
    def load_report(envelope: Mapping[str, Any]) -> dict[str, Any]:
        data = envelope.get("data") or {}
        path = data.get("report_path")
        if not path:
            raise AwrError("awr build envelope has no data.report_path", envelope)
        return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Be lenient about stray lines before the document: take the last object.
        start = text.rfind("\n{")
        if start < 0:
            return None
        try:
            value = json.loads(text[start + 1:])
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or "exit_code" not in value:
        return None
    return value
