"""One poller per bot token: a host-wide lock per (channel, token) (spec 005 FR-041/FR-042).

Before a token channel (Telegram, Discord) starts, the channel manager takes an
exclusive ``flock`` on ``<run dir>/channel-locks/<channel>-<h>.lock``, where ``<h>`` is
the first 16 hex digits of SHA-256 of the token, and writes ``<same>.json`` with
``pid``, ``workspace``, ``config`` and ``started_at``. Neither file holds the token or
its full hash. A second instance configured with the same token sees the lock held and
does not start that channel at all (no network call); its other channels start
normally. The OS drops the lock when the holder exits.

The run dir is ``$MOEKA_RUN_DIR``, else ``$XDG_RUNTIME_DIR/moeka``, else
``/tmp/moeka-<uid>`` (mode 0700). Two containers that do not share a run dir cannot
see each other's locks; Telegram's ``Conflict`` handling covers that case.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Channels whose bot token admits only one consumer at a time.
TOKEN_LOCK_CHANNELS = frozenset({"telegram", "discord"})

_REGISTRY: dict[Path, ChannelTokenLock] = {}
_REGISTRY_LOCK = threading.Lock()


class ChannelTokenInUseError(RuntimeError):
    """Another process (or channel manager) holds this channel's token lock.

    ``holder`` is the sidecar record (``pid``, ``workspace``, ``config``,
    ``started_at``) or ``None`` when it cannot be read.
    """

    def __init__(self, channel: str, holder: dict[str, Any] | None, lock_path: Path) -> None:
        self.channel = channel
        self.holder = holder
        self.lock_path = lock_path
        where = ""
        if holder:
            parts = []
            if holder.get("workspace"):
                parts.append(f"workspace {holder['workspace']}")
            if holder.get("pid") is not None:
                parts.append(f"pid {holder['pid']}")
            if parts:
                where = " by " + ", ".join(parts)
        super().__init__(
            f"channel_token_in_use: the {channel} bot token is already in use{where}; "
            f"this instance does not start {channel} (one poller per token)"
        )


def token_digest(token: str) -> str:
    """The first 16 hex digits of SHA-256 of *token* (the lock file key)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def lockable_token(token: object) -> str | None:
    """*token* stripped, or ``None`` when empty or still an unexpanded ``${VAR}``."""
    if not isinstance(token, str):
        return None
    value = token.strip()
    if not value or "${" in value:
        return None
    return value


def default_run_dir() -> Path:
    from nanobot.config.instances import run_dir

    return run_dir()


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class ChannelTokenLock:
    """A held token lock; ``release()`` (or the context manager) drops it once."""

    def __init__(self, channel: str, path: Path, fd: int, owner: dict[str, Any]) -> None:
        self.channel = channel
        self.path = path
        self.sidecar = path.with_suffix(".json")
        self.owner = owner
        self._fd: int | None = fd
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                current = _read_sidecar(self.sidecar)
                if current is not None and current.get("pid") == self.owner.get("pid") \
                        and current.get("started_at") == self.owner.get("started_at"):
                    with contextlib.suppress(OSError):
                        self.sidecar.unlink()
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
                with _REGISTRY_LOCK:
                    if _REGISTRY.get(self.path) is self:
                        del _REGISTRY[self.path]

    def __enter__(self) -> ChannelTokenLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"ChannelTokenLock({self.channel!r}, {str(self.path)!r}, held={self.held})"


def acquire_channel_token_lock(
    channel: str,
    token: str,
    *,
    run_dir: str | Path | None = None,
    owner: dict[str, Any] | None = None,
) -> ChannelTokenLock:
    """Take the token lock for *channel* or raise :class:`ChannelTokenInUseError`.

    *owner* adds fields to the sidecar (``workspace``, ``config``); ``pid`` and
    ``started_at`` are filled in. Never blocks. Raises ``ValueError`` for an empty or
    unexpanded token (such a channel takes no lock).
    """
    value = lockable_token(token)
    if value is None:
        raise ValueError("an empty or unexpanded (${...}) token takes no lock")
    base = Path(run_dir) if run_dir is not None else default_run_dir()
    locks_dir = base / "channel-locks"
    locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in channel) or "channel"
    path = locks_dir / f"{name}-{token_digest(value)}.lock"
    sidecar = path.with_suffix(".json")
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(path)
        if existing is not None and existing.held:
            raise ChannelTokenInUseError(channel, dict(existing.owner), path)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise ChannelTokenInUseError(channel, _read_sidecar(sidecar), path) from None
            raise
        record: dict[str, Any] = {
            "pid": os.getpid(),
            "workspace": None,
            "config": None,
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for key, val in (owner or {}).items():
            if key in ("pid", "started_at"):
                continue
            record[key] = str(val) if isinstance(val, Path) else val
        record["channel"] = channel
        tmp = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, sidecar)
        except OSError:
            pass
        lock = ChannelTokenLock(channel, path, fd, record)
        _REGISTRY[path] = lock
        return lock


__all__ = [
    "TOKEN_LOCK_CHANNELS",
    "ChannelTokenInUseError",
    "ChannelTokenLock",
    "acquire_channel_token_lock",
    "lockable_token",
    "token_digest",
]
