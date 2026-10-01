"""One writer per state dir: the per-state-dir instance lock (spec 005, FR-033..FR-036).

A writer (``moeka.Kernel(attach="write")``, ``nanobot gateway``, ``nanobot serve``,
one-shot and ``--classic`` ``nanobot agent``) holds an exclusive ``flock`` on
``<state_dir>/.instance.lock`` for its lifetime and records itself in
``<state_dir>/.instance.json`` (``pid``, ``hostname``, ``started_at``, ``argv0``,
``mode``). A second writer, in another process or in this one (also through a path
that resolves to the same directory), is refused at once with
:class:`InstanceLockedError` and writes nothing. The OS drops the lock when the
holder exits, SIGKILL included.

No ambient reads: the state dir is always an argument (invariant I1).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_FILENAME = ".instance.lock"
HOLDER_FILENAME = ".instance.json"

_HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


class InstanceLockedError(RuntimeError):
    """Another writer holds this state dir, or it cannot be locked at all.

    ``state_dir`` is the resolved directory, ``holder`` the holder record from
    ``.instance.json`` (``None`` when unreadable) and ``reason`` is ``"held"`` or
    ``"lock_unsupported"`` (the filesystem has no ``flock``; the writer refuses to
    run unlocked).
    """

    def __init__(
        self,
        state_dir: Path,
        holder: dict[str, Any] | None = None,
        *,
        reason: str = "held",
        detail: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.holder = holder
        self.reason = reason
        if reason == "lock_unsupported":
            message = (
                f"state dir {self.state_dir} cannot be locked (lock_unsupported"
                f"{': ' + detail if detail else ''}); refusing to run a writer unlocked"
            )
        else:
            message = f"state dir {self.state_dir} is locked by another writer"
            if holder and holder.get("pid") is not None:
                where = holder.get("hostname")
                mode = holder.get("mode")
                extra = ", ".join(
                    part for part in (
                        f"host {where}" if where else "",
                        f"mode {mode}" if mode else "",
                        f"since {holder['started_at']}" if holder.get("started_at") else "",
                    ) if part
                )
                message += f" (pid {holder['pid']}{', ' + extra if extra else ''})"
            message += "; stop it, or attach read-only"
        super().__init__(message)


class ReadOnlyKernelError(RuntimeError):
    """A mutating call on a read-only Kernel, or a read-only attach to a missing state dir."""


_REGISTRY: dict[Path, InstanceLock] = {}
_REGISTRY_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_holder(state_dir: str | Path) -> dict[str, Any] | None:
    """The holder record in ``<state_dir>/.instance.json`` (``None`` if unreadable)."""
    try:
        data = json.loads((Path(state_dir) / HOLDER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class InstanceLock:
    """A held instance lock; :meth:`release` (or the context manager) drops it once."""

    def __init__(self, state_dir: Path, fd: int, holder: dict[str, Any]) -> None:
        self.state_dir = state_dir
        self.holder = holder
        self._fd: int | None = fd
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return str(self.holder.get("mode", "write"))

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Remove the holder record, unlock and close. Idempotent."""
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                current = read_holder(self.state_dir)
                if current is not None and current.get("pid") == self.holder.get("pid") \
                        and current.get("started_at") == self.holder.get("started_at"):
                    with contextlib.suppress(OSError):
                        (self.state_dir / HOLDER_FILENAME).unlink()
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
                with _REGISTRY_LOCK:
                    if _REGISTRY.get(self.state_dir) is self:
                        del _REGISTRY[self.state_dir]

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "held" if self.held else "released"
        return f"InstanceLock({str(self.state_dir)!r}, {state})"


def _write_holder(state_dir: Path, holder: dict[str, Any]) -> None:
    path = state_dir / HOLDER_FILENAME
    tmp = state_dir / f".{HOLDER_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(holder, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def acquire_instance_lock(
    state_dir: str | Path, *, mode: str = "write", create: bool = True,
) -> InstanceLock:
    """Take the exclusive lock on *state_dir* or raise :class:`InstanceLockedError`.

    Never blocks. A refused writer creates and modifies nothing. *create* makes the
    state dir when it is missing (a writer owns it).
    """
    raw = Path(state_dir)
    if create:
        raw.mkdir(parents=True, exist_ok=True)
    resolved = raw.resolve()
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(resolved)
        if existing is not None and existing.held:
            raise InstanceLockedError(resolved, dict(existing.holder), reason="held")
        lock_path = resolved / LOCK_FILENAME
        try:
            fd = os.open(
                str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644,
            )
        except OSError as exc:
            raise InstanceLockedError(
                resolved, None, reason="lock_unsupported", detail=str(exc),
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _HELD_ERRNOS:
                raise InstanceLockedError(resolved, read_holder(resolved), reason="held") from None
            # ENOLCK / EOPNOTSUPP / EINVAL / ENOSYS ...: no flock on this filesystem.
            raise InstanceLockedError(
                resolved, None, reason="lock_unsupported",
                detail=os.strerror(exc.errno or 0) or str(exc),
            ) from exc
        holder = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": _now_iso(),
            "argv0": sys.argv[0] if sys.argv else "",
            "mode": mode,
        }
        try:
            _write_holder(resolved, holder)
        except OSError:
            # The lock itself is what excludes writers; the record is diagnostics.
            pass
        lock = InstanceLock(resolved, fd, holder)
        _REGISTRY[resolved] = lock
        return lock


def held_locks() -> list[InstanceLock]:
    """Locks this process holds (diagnostics and tests)."""
    with _REGISTRY_LOCK:
        return [lock for lock in _REGISTRY.values() if lock.held]


__all__ = [
    "HOLDER_FILENAME",
    "LOCK_FILENAME",
    "InstanceLock",
    "InstanceLockedError",
    "ReadOnlyKernelError",
    "acquire_instance_lock",
    "held_locks",
    "read_holder",
]
