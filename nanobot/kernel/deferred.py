"""Scratchpad and deferred-action log (design section 5a).

- The agent's scratchpad is ``<work_dir>/scratchpad/``: free-form notes it owns.
- The deferred-action log is ``<work_dir>/scratchpad/deferred.jsonl``: one JSON object
  per line, ``{ts, tool, arguments, reason, capability, source}``. ``source`` is
  ``"gate"`` (appended automatically for a denied call) or ``"agent"`` (the
  ``defer_action`` tool). An entry never executes and never grants anything.
- Writes are direct (``DeferredLog.append``): they never pass the capability gate, are
  never classified as a denial and never count toward the I5 denial ceiling.
- ``arguments`` and ``reason`` are redacted with ``nanobot.security.redact`` before
  they reach disk, plus values under secret-looking keys (``api_key``, ``token``, ...).
- Size cap with numbered rotation: past ``max_bytes`` the file moves to
  ``deferred.jsonl.1`` (``.1`` -> ``.2`` ...), keeping ``backups`` old files.
- This is not the audit stream: the audit stream is the host's trace sink in
  ``state_dir``; the deferred log is agent-readable, agent-writable text and is read
  back with the untrusted banner (``read_file``).

Module-level imports are stdlib + loguru only; ``nanobot.security.redact`` is imported
lazily (kernel import rule, Ruling C).
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment

SCRATCHPAD_DIRNAME = "scratchpad"
DEFERRED_LOG_NAME = "deferred.jsonl"
# The phrase every logged denial carries (design 5a). Appended after the denial
# text, so the pinned marker phrases inside it stay byte-identical.
DEFERRED_NOTE = "logged as a deferred action; do not retry"

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUPS = 3
_MAX_REASON_CHARS = 2000
_MASK = "<redacted>"
_SECRET_KEY_RE = re.compile(r"(?i)(key|token|secret|password|passwd|bearer|authorization)")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def scratchpad_dir(work_dir: Path | str) -> Path:
    """``<work_dir>/scratchpad``."""
    return Path(work_dir) / SCRATCHPAD_DIRNAME


def deferred_log_path(work_dir: Path | str) -> Path:
    """``<work_dir>/scratchpad/deferred.jsonl``."""
    return scratchpad_dir(work_dir) / DEFERRED_LOG_NAME


def is_scratchpad_path(path: Path | str, work_dir: Path | str | None) -> bool:
    """True when *path* is inside ``<work_dir>/scratchpad`` (the rotated logs included)."""
    if work_dir is None:
        return False
    try:
        root = scratchpad_dir(Path(work_dir).expanduser()).resolve()
        Path(path).expanduser().resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def deferred_work_dir(
    env: CoreEnvironment | None, workspace: Path | str | None,
) -> Path | None:
    """Where a denial's entry goes: the host's ``work_dir``, else the explicit workspace."""
    if env is not None:
        return Path(env.paths.work_dir)
    return Path(workspace) if workspace is not None and str(workspace) else None


def with_deferred_note(text: str) -> str:
    """*text* with the deferred-action note appended (idempotent)."""
    if DEFERRED_NOTE in text:
        return text
    return f"{text.rstrip()}\n[{DEFERRED_NOTE}]"


def _redact_arguments(value: Any) -> Any:
    from nanobot.security.redact import redact_value

    def by_key(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                k: (_MASK if isinstance(k, str) and _SECRET_KEY_RE.search(k)
                    and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                    else by_key(v))
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [by_key(v) for v in item]
        return item

    return redact_value(by_key(value))


def _redact_reason(reason: str) -> str:
    from nanobot.security.redact import redact_text

    text = redact_text(str(reason or "")) or ""
    return text if len(text) <= _MAX_REASON_CHARS else text[:_MAX_REASON_CHARS] + "..."


class DeferredLog:
    """Append-only JSONL log of actions the agent wants to run but cannot.

    *max_bytes* and *backups* exist so tests can use a tiny cap; hosts keep the
    defaults (2 MiB, three rotated files).
    """

    def __init__(
        self,
        work_dir: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.max_bytes = max(1, int(max_bytes))
        self.backups = max(0, int(backups))

    @property
    def path(self) -> Path:
        return deferred_log_path(self.work_dir)

    def append(
        self,
        *,
        tool: str,
        arguments: Any,
        reason: str,
        capability: str | None,
        source: str,
    ) -> dict[str, Any]:
        """Write one entry and return it (as written). Raises ``OSError`` on I/O failure."""
        if source not in ("agent", "gate"):
            raise ValueError(f"source must be 'agent' or 'gate', got {source!r}")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": str(tool),
            "arguments": _redact_arguments(arguments if arguments is not None else {}),
            "reason": _redact_reason(reason),
            "capability": capability,
            "source": source,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        path = self.path
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                self._rotate(path)
            with open(path, "ab") as fh:
                fh.write(data)
        return json.loads(line)

    def _rotate(self, path: Path) -> None:
        if self.backups == 0:
            path.unlink(missing_ok=True)
            return
        for index in range(self.backups - 1, 0, -1):
            src = Path(f"{path}.{index}")
            if src.exists():
                os.replace(src, f"{path}.{index + 1}")
        os.replace(path, f"{path}.1")


def record_gate_denial(
    work_dir: Path | None,
    *,
    tool: str,
    arguments: Any,
    reason: str,
    capability: str | None,
) -> bool:
    """Append a ``source="gate"`` entry for a denied call; ``True`` when written.

    Never raises: a failing write only loses the entry (logged as a warning), it never
    changes the denial or the ceiling.
    """
    if work_dir is None:
        return False
    try:
        DeferredLog(work_dir).append(
            tool=tool, arguments=arguments, reason=reason,
            capability=capability, source="gate",
        )
    except Exception as exc:  # noqa: BLE001 - the denial must stand regardless
        logger.warning("could not append deferred entry for {} under {}: {!r}",
                       tool, work_dir, exc)
        return False
    return True


__all__ = [
    "DEFAULT_BACKUPS",
    "DEFAULT_MAX_BYTES",
    "DEFERRED_LOG_NAME",
    "DEFERRED_NOTE",
    "SCRATCHPAD_DIRNAME",
    "DeferredLog",
    "deferred_log_path",
    "deferred_work_dir",
    "is_scratchpad_path",
    "record_gate_denial",
    "scratchpad_dir",
    "with_deferred_note",
]
