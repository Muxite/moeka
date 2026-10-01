"""Session handles: host-ordered conversations that can checkpoint, fork and rewind.

``kernel.sessions`` (a :class:`Sessions`, built lazily) hands out :class:`Session`
handles over the kernel's one shared ``SessionManager`` (under ``state_dir``), the
same store agent runs use, so a handle and ``agent.run(..., session=handle)`` see
one transcript. Every mutation is saved before it returns, so sessions, their
messages and metadata survive a kernel restart on the same ``state_dir``.

- :meth:`Session.append` adds OpenAI-shape messages in the host's order (``name``
  marks a speaker) and returns a :class:`Checkpoint`: ``(key, n_messages, digest)``
  where ``digest`` is the sha256 of the canonical JSON of ``messages[:n]``.
- :meth:`Session.rewind` cuts the transcript back to a checkpoint (or a message
  count); :meth:`Session.fork` copies a prefix under a new key. A checkpoint whose
  digest no longer matches the current prefix raises :class:`CheckpointMismatch`,
  so a rewind never lands on a history that silently diverged. A checkpoint is
  checked by content only, so one taken on a parent also applies to its forks.
- Rewinds and forks go through ``SessionManager.truncate`` / ``fork_session``,
  which cut consolidation state to the kept prefix: a summary covering messages
  past the cut is dropped (offset reset to 0), ``provider_state``, the latest
  turn's usage and the in-flight turn keys are dropped. Built agents also forget
  their file-read state for the key. When no committed summary survives a rewind
  (and for a fork's target key and a deleted key), every agent's archived memory
  history for that key is dropped too (``Kernel._drop_session_history``).

Locking: runs, appends, rewinds, forks, restores and deletes on one key are
serialised by the kernel's per-key ``asyncio.Lock`` (all on the kernel loop).
``rewind``/``fork``/``delete`` never wait: if the key is busy (typically an agent
run is in progress on it) they raise :class:`SessionBusyError` immediately.
``append`` waits for the lock, up to its optional ``timeout`` (then
``SessionBusyError``). Calling ``append`` without a timeout from inside a run on
the same key (e.g. from an action) would wait for itself forever.

Reads (``messages``, ``metadata``, ``checkpoint()``, ``get``, ``list``, ``snapshot``)
never take the lock; they run on the kernel loop thread and see the latest saved or
in-flight state. Every ``async`` method has a ``*_sync`` twin that is safe from any
thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from nanobot.kernel.frozen import FrozenMap, freeze
from nanobot.sdk.types import SessionSnapshot

if TYPE_CHECKING:
    from nanobot.kernel.kernel import Kernel
    from nanobot.session.manager import Session as StoredSession
    from nanobot.session.manager import SessionManager

T = TypeVar("T")

_ROLES = frozenset({"system", "user", "assistant", "tool"})
# The in-flight/UI keys a kernel fork drops (not "title": host metadata is kept).
_FORK_DROP_METADATA = (
    "goal_state",
    "pending_user_turn",
    "pending_user_followups",
    "runtime_checkpoint",
    "session_handle",
    "webui_recovery",
    "thread_goal",
)


class SessionBusyError(RuntimeError):
    """The session key is in use (a run or another mutation holds its lock)."""


class CheckpointMismatch(ValueError):  # noqa: N818 - contract name
    """The session's current prefix no longer matches the checkpoint's digest."""


def _digest(messages: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        messages, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    """A point in a session: its first ``n_messages`` messages, identified by digest."""

    key: str
    n_messages: int
    digest: str


@dataclass(frozen=True)
class SessionInfo:
    """One row of :meth:`Sessions.list`."""

    key: str
    n_messages: int
    updated_at: str | None
    metadata: Mapping[str, Any]


def _prepare(raw: Any) -> tuple[str, Any, dict[str, Any]]:
    """Validate one host message and split it into ``add_message`` arguments."""
    from nanobot.runtime_context import RUNTIME_CONTEXT_HISTORY_META

    if not isinstance(raw, Mapping):
        raise TypeError(f"append() takes message mappings, got {type(raw).__name__}")
    role = raw.get("role")
    if role not in _ROLES:
        raise ValueError(f"message role must be one of {sorted(_ROLES)}, got {role!r}")
    if "content" not in raw and not (role == "assistant" and raw.get("tool_calls")):
        raise ValueError(f"a {role} message needs content")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError(f"message name must be a str, got {type(name).__name__}")
    # Trusted runtime context is only ever added by the agent loop itself.
    extra = {
        key: _plain(value) for key, value in raw.items()
        if key not in ("role", "content", RUNTIME_CONTEXT_HISTORY_META)
    }
    return role, _plain(raw.get("content")), extra


def _plain(value: Any) -> Any:
    """A deep, JSON-shaped copy: mappings -> dicts, lists/tuples -> lists."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return deepcopy(value)


class _LoopCalls:
    """Run store work on the kernel loop thread (directly when already on it)."""

    _kernel: Kernel

    def _call(self, fn: Callable[[], T]) -> T:
        bridge = self._kernel._bridge
        if threading.current_thread() is bridge._thread:
            return fn()

        async def go() -> T:
            return fn()

        return bridge.run(go())

    async def _on_loop(self, coro: Awaitable[T]) -> T:
        bridge = self._kernel._bridge
        if threading.current_thread() is bridge._thread:
            return await coro
        return await asyncio.wrap_future(bridge.submit(coro))

    def _sync(self, coro: Awaitable[T]) -> T:
        return self._kernel._bridge.run(coro)

    def _manager(self) -> SessionManager:
        return self._kernel._session_manager()

    def _stored(self, key: str) -> StoredSession:
        """The live (cached) stored session for *key*; ``KeyError`` if there is none."""
        manager = self._manager()
        session = manager.get_cached(key)
        if session is None:
            if manager.read_session_metadata(key) is None:
                raise KeyError(f"session {key!r} does not exist")
            session = manager.get_or_create(key)
        return session

    def _exists(self, key: str) -> bool:
        manager = self._manager()
        return manager.get_cached(key) is not None or \
            manager.read_session_metadata(key) is not None

    def _busy(self, key: str, op: str) -> asyncio.Lock:
        """The key's lock, refusing (without waiting) when it is held."""
        lock = self._kernel._session_lock(key)
        if lock.locked():
            raise SessionBusyError(
                f"cannot {op} session {key!r}: it is in use (a run is in progress)"
            )
        return lock

    def _forget_file_state(self, key: str) -> None:
        with self._kernel._agents_lock:
            agents = list(self._kernel._agents.values())
        for agent in agents:
            loop = agent._loop
            if loop is not None:
                loop.discard_session_file_state(key)


class Session(_LoopCalls):
    """A handle on one stored session (cheap; holds only the kernel and the key)."""

    def __init__(self, kernel: Kernel, key: str) -> None:
        self._kernel = kernel
        self._key = key

    @property
    def key(self) -> str:
        return self._key

    # -- reads ---------------------------------------------------------------

    @property
    def messages(self) -> tuple[Mapping[str, Any], ...]:
        """A fresh read-only copy of the transcript (``FrozenMap`` per message)."""
        return self._call(lambda: tuple(freeze(m) for m in self._stored(self._key).messages))

    @property
    def metadata(self) -> Mapping[str, Any]:
        """A fresh read-only copy of the session metadata."""
        return self._call(lambda: FrozenMap(self._stored(self._key).metadata))

    def _checkpoint_now(self, n: int | None = None) -> Checkpoint:
        messages = self._stored(self._key).messages
        n = len(messages) if n is None else n
        return Checkpoint(self._key, n, _digest(messages[:n]))

    def checkpoint(self) -> Checkpoint:
        """A checkpoint at the current end of the transcript."""
        return self._call(self._checkpoint_now)

    def snapshot(self) -> SessionSnapshot:
        """A serialisable full snapshot (``to_dict()``), for :meth:`Sessions.restore`."""
        from nanobot.sdk.types import snapshot_from_session

        return self._call(lambda: snapshot_from_session(
            self._stored(self._key), include_runtime_context=True,
        ))

    def _resolve(self, to: Checkpoint | int | None) -> int:
        """Message count for *to*, verifying a checkpoint against the current prefix."""
        messages = self._stored(self._key).messages
        if to is None:
            return len(messages)
        if isinstance(to, Checkpoint):
            n = to.n_messages
            if n > len(messages) or _digest(messages[:n]) != to.digest:
                raise CheckpointMismatch(
                    f"session {self._key!r} no longer has the history of checkpoint "
                    f"{to.key!r}@{n}"
                )
            return n
        if isinstance(to, bool) or not isinstance(to, int):
            raise TypeError(f"expected a Checkpoint or int, got {type(to).__name__}")
        if not 0 <= to <= len(messages):
            raise ValueError(f"message count must be in 0..{len(messages)}, got {to}")
        return to

    # -- mutations -------------------------------------------------------------

    async def append(self, *messages: Mapping[str, Any],
                     timeout: float | None = None) -> Checkpoint:
        """Append *messages* in order and save; returns the checkpoint after them.

        Each is an OpenAI-shape mapping with ``role`` in system/user/assistant/tool
        and ``content`` (an assistant message with ``tool_calls`` may omit it); a
        ``name`` field marks the speaker. Waits for the key's lock (an in-progress
        run), up to *timeout* seconds if given, then raises :class:`SessionBusyError`.
        """
        self._kernel._refuse_write("session.append")
        prepared = [_prepare(m) for m in messages]
        return await self._on_loop(self._append(prepared, timeout))

    async def _append(self, prepared: list[tuple[str, Any, dict[str, Any]]],
                      timeout: float | None) -> Checkpoint:
        lock = self._kernel._session_lock(self._key)
        try:
            async with asyncio.timeout(timeout):
                await lock.acquire()
        except TimeoutError as exc:
            raise SessionBusyError(
                f"session {self._key!r} stayed busy for {timeout}s"
            ) from exc
        try:
            session = self._stored(self._key)
            for role, content, extra in prepared:
                session.add_message(role, content, **extra)
            self._manager().save(session)
            return self._checkpoint_now()
        finally:
            lock.release()

    def append_sync(self, *messages: Mapping[str, Any],
                    timeout: float | None = None) -> Checkpoint:
        """Blocking :meth:`append`."""
        self._kernel._refuse_write("session.append_sync")
        prepared = [_prepare(m) for m in messages]
        return self._sync(self._append(prepared, timeout))

    async def rewind(self, to: Checkpoint | int) -> None:
        """Cut the transcript back to *to* (a checkpoint or a message count) and save.

        Raises :class:`SessionBusyError` if the key is in use and
        :class:`CheckpointMismatch` if the checkpoint's prefix has diverged.
        """
        self._kernel._refuse_write("session.rewind")
        await self._on_loop(self._rewind(to))

    async def _rewind(self, to: Checkpoint | int) -> None:
        lock = self._busy(self._key, "rewind")
        async with lock:
            n = self._resolve(to)
            self._manager().truncate(self._key, n)
            self._forget_file_state(self._key)

    def rewind_sync(self, to: Checkpoint | int) -> None:
        """Blocking :meth:`rewind`."""
        self._kernel._refuse_write("session.rewind_sync")
        self._sync(self._rewind(to))

    async def fork(self, at: Checkpoint | int | None = None, *,
                   key: str | None = None) -> Session:
        """A new session holding this one's first *at* messages (default: all).

        *key* defaults to ``"<this key>/fork-<8 hex>"``; an existing key raises
        ``ValueError``. The copy keeps the metadata (minus in-flight turn state) and
        the consolidation state that lies within the prefix. Raises
        :class:`SessionBusyError` if this session is in use.
        """
        self._kernel._refuse_write("session.fork")
        return await self._on_loop(self._fork(at, key))

    async def _fork(self, at: Checkpoint | int | None, key: str | None) -> Session:
        target = key if key is not None else f"{self._key}/fork-{uuid.uuid4().hex[:8]}"
        _check_key(target)
        lock = self._busy(self._key, "fork")
        target_lock = self._busy(target, "fork into")
        async with lock, target_lock:
            n = self._resolve(at)
            if self._exists(target):
                raise ValueError(f"session {target!r} already exists")
            self._manager().fork_session(
                self._key, target, upto_message=n,
                strip_runtime_context=False, drop_metadata=_FORK_DROP_METADATA,
            )
        return Session(self._kernel, target)

    def fork_sync(self, at: Checkpoint | int | None = None, *,
                  key: str | None = None) -> Session:
        """Blocking :meth:`fork`."""
        self._kernel._refuse_write("session.fork_sync")
        return self._sync(self._fork(at, key))

    async def set_metadata(self, **kv: Any) -> None:
        """Merge *kv* into the metadata and save (waits for the key's lock)."""
        self._kernel._refuse_write("session.set_metadata")
        await self._on_loop(self._set_metadata(kv))

    async def _set_metadata(self, kv: dict[str, Any]) -> None:
        async with self._kernel._session_lock(self._key):
            session = self._stored(self._key)
            session.metadata.update(_plain(kv))
            self._manager().save(session)

    def set_metadata_sync(self, **kv: Any) -> None:
        """Blocking :meth:`set_metadata`."""
        self._kernel._refuse_write("session.set_metadata_sync")
        self._sync(self._set_metadata(kv))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Session):
            return other._kernel is self._kernel and other._key == self._key
        return NotImplemented

    def __hash__(self) -> int:
        return hash((id(self._kernel), self._key))

    def __repr__(self) -> str:
        return f"Session({self._key!r})"


def _check_key(key: Any) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError(f"a session key must be a non-empty str, got {key!r}")


class Sessions(_LoopCalls):
    """``kernel.sessions``: create, open, list, restore and delete session handles."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    async def create(self, key: str | None = None,
                     metadata: Mapping[str, Any] | None = None) -> Session:
        """A new, saved, empty session (*key* default ``"session-<uuid>"``).

        An existing key raises ``ValueError``.
        """
        self._kernel._refuse_write("sessions.create")
        return await self._on_loop(self._create(key, metadata))

    async def _create(self, key: str | None, metadata: Mapping[str, Any] | None) -> Session:
        key = key if key is not None else f"session-{uuid.uuid4().hex}"
        _check_key(key)
        async with self._busy(key, "create"):
            if self._exists(key):
                raise ValueError(f"session {key!r} already exists")
            manager = self._manager()
            session = manager.get_or_create(key)
            if metadata:
                session.metadata.update(_plain(metadata))
            manager.save(session)
        return Session(self._kernel, key)

    def create_sync(self, key: str | None = None,
                    metadata: Mapping[str, Any] | None = None) -> Session:
        """Blocking :meth:`create`."""
        self._kernel._refuse_write("sessions.create_sync")
        return self._sync(self._create(key, metadata))

    def get(self, key: str) -> Session | None:
        """The handle for an existing session, else ``None``."""
        return Session(self._kernel, key) if self._call(lambda: self._exists(key)) else None

    def open(self, key: str) -> Session:
        """The handle for *key*, creating (and saving) an empty session if needed."""
        self._kernel._refuse_write("sessions.open")
        _check_key(key)

        def get_or_create() -> None:
            if not self._exists(key):
                manager = self._manager()
                manager.save(manager.get_or_create(key))

        self._call(get_or_create)
        return Session(self._kernel, key)

    def list(self) -> list[SessionInfo]:
        """Every stored session, most recently updated first."""

        def rows() -> list[SessionInfo]:
            manager = self._manager()
            out = []
            for row in manager.list_sessions():
                key = str(row.get("key") or "")
                session = manager.get_cached(key) or manager.read_session_snapshot(key)
                if session is None:
                    continue
                out.append(SessionInfo(
                    key=key,
                    n_messages=len(session.messages),
                    updated_at=session.updated_at.isoformat(),
                    metadata=FrozenMap(session.metadata),
                ))
            return out

        return self._call(rows)

    async def restore(self, snapshot: SessionSnapshot, *, key: str | None = None) -> Session:
        """A new session from *snapshot* (under *key*, default the snapshot's key).

        The target must not exist (``ValueError``). Messages, their timestamps and
        the metadata are restored as they were; a committed summary's offset is
        recovered from the transcript's latest summary marker.
        """
        self._kernel._refuse_write("sessions.restore")
        return await self._on_loop(self._restore(snapshot, key))

    async def _restore(self, snapshot: SessionSnapshot, key: str | None) -> Session:
        from nanobot.sdk.clients import SessionClient
        from nanobot.session.summary import is_summary_checkpoint

        if not isinstance(snapshot, SessionSnapshot):
            raise TypeError(f"restore() needs a SessionSnapshot, got {type(snapshot).__name__}")
        target = key if key is not None else snapshot.key
        _check_key(target)
        manager = self._manager()
        async with self._busy(target, "restore into"):
            if self._exists(target):
                raise ValueError(f"session {target!r} already exists")
            try:
                await SessionClient(_ManagerHost(manager)).restore(
                    snapshot, session_key=target, save=False,
                )
            except BaseException:
                manager.invalidate(target)
                raise
            session = manager.get_or_create(target)
            if "_last_summary" in session.metadata:
                markers = [i for i, m in enumerate(session.messages) if is_summary_checkpoint(m)]
                session.last_archived = markers[-1] if markers else 0
            manager.save(session, fsync=True)
        return Session(self._kernel, target)

    def restore_sync(self, snapshot: SessionSnapshot, *, key: str | None = None) -> Session:
        """Blocking :meth:`restore`."""
        self._kernel._refuse_write("sessions.restore_sync")
        return self._sync(self._restore(snapshot, key))

    async def delete(self, key: str) -> bool:
        """Delete a session; ``True`` if one existed. Refused while the key is in use."""
        self._kernel._refuse_write("sessions.delete")
        return await self._on_loop(self._delete(key))

    async def _delete(self, key: str) -> bool:
        async with self._busy(key, "delete"):
            deleted = self._manager().delete_session(key)
            self._forget_file_state(key)
        # A handle kept by the host recreates nothing: reads raise KeyError.
        if not self._kernel._session_lock(key).locked():
            self._kernel._session_locks.pop(key, None)
        return deleted

    def delete_sync(self, key: str) -> bool:
        """Blocking :meth:`delete`."""
        self._kernel._refuse_write("sessions.delete_sync")
        return self._sync(self._delete(key))

    def __repr__(self) -> str:
        return f"Sessions({self._kernel!r})"


class _ManagerHost:
    """The one attribute ``SessionClient`` needs from its loop: ``.sessions``."""

    def __init__(self, sessions: SessionManager) -> None:
        self.sessions = sessions


__all__ = [
    "Checkpoint",
    "CheckpointMismatch",
    "Session",
    "SessionBusyError",
    "SessionInfo",
    "SessionSnapshot",
    "Sessions",
]
