"""Per-backend drivers: build the command line and read the event stream.

The two backends are different accounts with different usage, so each has its own driver
and moeka never substitutes one for the other. A driver is pure: it builds argv and turns
one stream line into a :class:`WorkerEvent`. It never starts a process.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PREAMBLE = (
    "You are doing a job for Moeka, the owner's assistant, inside this project directory.\n"
    "Work only in this directory. Do not push, merge, deploy, publish, or change permissions "
    "or credentials. Do not read or write .env files.\n"
    "Do not guess requirements. If something is ambiguous or needs a decision only the "
    "owner can make, stop and say what you need.\n"
    "When you finish, end your final message with exactly one line:\n"
    "MOEKA_STATUS: completed   (or)   MOEKA_STATUS: needs_user   (or)   MOEKA_STATUS: blocked\n"
    "Before that line, say what you did, which checks you ran and their result, and what "
    "remains.\n\n"
)
_STATUS_RE = re.compile(r"MOEKA_STATUS:\s*(completed|needs_user|blocked)", re.IGNORECASE)


def wrap_request(request: str, context: str = "") -> str:
    ctx = f"Relevant context from the owner's history:\n{context.strip()}\n\n" if context.strip() else ""
    return f"{PREAMBLE}{ctx}The owner's request, verbatim:\n{request.strip()}"


def split_status(text: str) -> tuple[str, str | None]:
    """(text without the status line, status or None when the worker omitted it)."""
    matches = list(_STATUS_RE.finditer(text or ""))
    if not matches:
        return (text or "").strip(), None
    last = matches[-1]
    body = (text[: last.start()] + text[last.end():]).strip()
    return body, last.group(1).lower()


@dataclass(slots=True)
class WorkerEvent:
    kind: str  # session | progress | message | result | error | rate_limit
    session_id: str = ""
    text: str = ""
    is_error: bool = False
    cause: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    permission_denials: list[str] = field(default_factory=list)


class Driver:
    backend = ""

    def argv(
        self, binary: str, prompt: str, session_id: str | None, extra: list[str],
        disallowed: list[str],
    ) -> list[str]:
        raise NotImplementedError

    def parse(self, line: str) -> WorkerEvent | None:
        raise NotImplementedError

    @staticmethod
    def _json(line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


class ClaudeDriver(Driver):
    backend = "claude"

    def argv(self, binary, prompt, session_id, extra, disallowed):
        args = [
            binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            # The owner's hooks, plugins and MCP servers belong to the owner's own sessions.
            "--setting-sources", "project,local", "--strict-mcp-config",
            "--allowedTools", "Bash", "Edit", "Write", "Read", "Glob", "Grep",
        ]
        if disallowed:
            args += ["--disallowedTools", *disallowed]
        if session_id:
            args += ["--resume", session_id]
        return [*args, *extra]

    def parse(self, line: str) -> WorkerEvent | None:
        e = self._json(line)
        if e is None:
            return None
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            return WorkerEvent("session", session_id=str(e.get("session_id") or ""))
        if kind == "assistant":
            parts = (e.get("message") or {}).get("content") or []
            text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
            tools = [p.get("name", "") for p in parts if p.get("type") == "tool_use"]
            note = text or (f"using {', '.join(tools)}" if tools else "")
            return WorkerEvent("progress", text=note[:300]) if note else None
        if kind == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            return WorkerEvent("rate_limit", text=str(info.get("status", "")), usage={
                "window": info.get("rateLimitType", ""),
                "five_hour": (info.get("unifiedWindows") or {}).get("five_hour", {}).get("utilization"),
                "seven_day": (info.get("unifiedWindows") or {}).get("seven_day", {}).get("utilization"),
            })
        if kind == "result":
            usage = e.get("usage") or {}
            denials = [str(d.get("tool_name", d)) if isinstance(d, dict) else str(d)
                       for d in e.get("permission_denials") or []]
            return WorkerEvent(
                "result", session_id=str(e.get("session_id") or ""),
                text=str(e.get("result") or ""), is_error=bool(e.get("is_error")),
                cause=str(e.get("subtype") or "") if e.get("is_error") else "",
                usage={"cost_usd": e.get("total_cost_usd") or 0,
                       "input_tokens": usage.get("input_tokens", 0),
                       "output_tokens": usage.get("output_tokens", 0),
                       "terminal_reason": e.get("terminal_reason", "")},
                permission_denials=denials,
            )
        return None


class CodexDriver(Driver):
    backend = "codex"

    def argv(self, binary, prompt, session_id, extra, disallowed):
        common = ["--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"']
        if session_id:
            return [binary, "exec", "resume", session_id, *common, *extra, prompt]
        return [binary, "exec", *common, *extra, prompt]

    def parse(self, line: str) -> WorkerEvent | None:
        e = self._json(line)
        if e is None:
            return None
        kind = e.get("type")
        if kind == "thread.started":
            return WorkerEvent("session", session_id=str(e.get("thread_id") or ""))
        if kind in {"item.started", "item.completed"}:
            item = e.get("item") or {}
            if item.get("type") == "agent_message" and kind == "item.completed":
                return WorkerEvent("message", text=str(item.get("text", ""))[:8000])
            if item.get("type") == "command_execution" and kind == "item.started":
                return WorkerEvent("progress", text=f"running {str(item.get('command', ''))[:200]}")
            return None
        if kind == "turn.completed":
            u = e.get("usage") or {}
            return WorkerEvent("result", usage={
                "cost_usd": 0, "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0)})
        if kind in {"turn.failed", "error"}:
            msg = e.get("message") or (e.get("error") or {}).get("message") or "codex error"
            return WorkerEvent("error", text=str(msg)[:400], is_error=True, cause="codex_error")
        return None


DRIVERS: dict[str, Driver] = {"claude": ClaudeDriver(), "codex": CodexDriver()}
