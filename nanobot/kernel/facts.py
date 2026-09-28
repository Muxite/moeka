"""Generic epistemic fact store: values with provenance (Task 22, design I3 / section 7).

Contract:
- :meth:`FactStore.record` stores one value with its provenance and returns a new
  trace ID; :meth:`FactStore.resolve` maps a trace ID back to its :class:`FactRecord`,
  or ``None`` when the ID points to nothing (the cite check an artifact store runs).
- ``source_kind`` is a CLOSED set (:data:`SOURCE_KINDS`: ``document``, ``tool``,
  ``user``), per design section 7: a committed value cites a document span, a tool
  output or a user turn. Anything else raises ``ValueError``.
- ``source_ref`` names the source (document ID, tool call ID, session turn ID); it
  must be a non-empty string. ``span`` is optional free-form text locating the
  value inside the source (a character range, a JSON pointer, ...).
- ``value`` is any JSON value; it is stored as JSON text (``allow_nan=False``) and
  decoded on resolve. A non-JSON value raises ``ValueError`` and records nothing.
- Generic mechanics only: no domain schemas, no dedupe, no update or delete. A fact
  is immutable once recorded.
- :meth:`FactStore.query` lists facts in creation order, filtered by a ``source_ref``
  prefix and/or ``source_kind``. The subject of a fact is carried by the ref (a
  convention such as ``person:alice/relationship``; no subject column exists), so
  ``query(ref_prefix="person:alice/")`` is "everything recorded about alice".

Trace IDs:
- Format ``fact-<32 hex>`` from ``uuid4`` (122 random bits).
- Random, not a content hash: the same value from two sources (or twice from one
  source) is two provenance records, not one.
- Random, not a counter or time-ordered ULID: an ID leaks neither order, time nor
  volume, and cannot be guessed. A model citing an ID must have been handed it by a
  real record; it cannot mint a plausible one.

Storage:
- ``<state_dir>/facts.db`` (:data:`FACTS_DB_FILENAME`). ``state_dir``, not
  ``data_dir``: this is host-owned kernel state, like the plugin registry
  (``kernel-plugins.json``, Task 18). ``data_dir`` holds the legacy auth/plugin-data
  subtrees and can be overridden to the legacy instance data dir.
- SQLite in WAL mode, ``busy_timeout`` and connect timeout, one connection per
  instance: the same setup as ``nanobot/session/sqlite_store.py``. Each ``record`` is
  one committed INSERT, so a crash never leaves a half-written fact.
- ``PRAGMA user_version`` holds :data:`SCHEMA_VERSION`. A fresh file is created at
  that version inside one ``BEGIN IMMEDIATE`` transaction (concurrent first opens
  converge). A file with a NEWER version is refused with :class:`FactStoreError`
  (fail closed, never downgrade).
- Version 2 (Task 11) adds the ``facts_ref_idx`` index on ``source_ref`` for
  :meth:`FactStore.query`'s prefix scans. A version-1 file is migrated on open, in the
  same ``BEGIN IMMEDIATE`` (every statement is ``IF NOT EXISTS``, so the step is
  idempotent and concurrent openers converge); the table itself is unchanged.
- The WAL switch is retried with backoff on ``database is locked``: SQLite skips
  the busy handler for a journal-mode change, so concurrent first opens of a fresh
  file otherwise fail at once (seen in the concurrent-process test).
- Concurrency: calls on one instance are serialised by a lock; separate instances
  and separate processes are serialised by SQLite's WAL locking plus the busy
  timeout. Tests cover shared-instance threads, per-thread instances and separate
  OS processes (no lost rows, ``integrity_check`` ok).
- The file floor denies the file tools read and write of the database and its
  ``-wal``/``-shm``/``-journal`` sidecars in both layouts
  (``nanobot/security/protected_paths.py``). ``exec`` is not stopped by the file
  floor (the documented exec caveat); real containment needs OS isolation.

Trace:
- A committed ``record`` emits ``{"event": "fact.recorded", "fact_id",
  "source_kind", "source_ref", "source_span"}`` through :func:`safe_emit` (the
  fact's id and source span; the stamp's ``trace_id``/``span`` are the run's). The
  value is never put on the trace. A rejected ``record`` emits nothing.

Wiring (what is live):
- :meth:`FactStore.from_env` builds the store a host gets from a ``CoreEnvironment``
  (``env.paths.state_dir`` and ``env.trace``). ``MoekaKernel.facts`` (Task 25) is that
  store, built lazily from the kernel's env; the host records facts through it.
  The public kernel's ``Kernel.epistemics`` (Task 11, :mod:`nanobot.kernel.epistemics`)
  wraps it (``record_fact`` / ``fact`` / ``facts``), built from ``kernel.core_env``.
- Nothing in the gateway, ``AgentLoop``, typed tool results or
  ``MoekaCore.ingest_text``/``retrieve`` records facts automatically; the host (or its
  actions) calls ``record``, and ``clarify.record_answer`` records ``user`` facts.

Imports: stdlib and ``nanobot.kernel.trace`` only; import-cheap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from nanobot.kernel.trace import NullTraceSink, TraceSink, safe_emit

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment

SourceKind = Literal["document", "tool", "user"]
SOURCE_KINDS: frozenset[str] = frozenset(get_args(SourceKind))

FACTS_DB_FILENAME = "facts.db"
SCHEMA_VERSION = 2
TRACE_ID_PREFIX = "fact-"

_CONNECT_TIMEOUT_S = 30.0
_BUSY_TIMEOUT_MS = 10_000


class FactStoreError(RuntimeError):
    """The fact database cannot be used (e.g. written by a newer schema)."""


@dataclass(frozen=True)
class FactRecord:
    """One recorded fact and its provenance."""

    trace_id: str
    source_kind: SourceKind
    source_ref: str
    span: str | None
    value: Any
    created_at: str


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch ``conn`` to WAL, retrying while other connections hold the file.

    SQLite does not run the busy handler for a journal-mode change, so when several
    connections open a fresh database at once the switch fails immediately with
    ``database is locked``. Retry with backoff for up to the busy timeout.
    """
    deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
    delay = 0.005
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if ("locked" not in message and "busy" not in message) or (
                time.monotonic() >= deadline
            ):
                raise
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


def _record(row: sqlite3.Row) -> FactRecord:
    return FactRecord(
        trace_id=row["trace_id"],
        source_kind=row["source_kind"],
        source_ref=row["source_ref"],
        span=row["span"],
        value=json.loads(row["value"]),
        created_at=row["created_at"],
    )


def _prefix_upper_bound(prefix: str) -> str | None:
    """The smallest string greater than every string starting with *prefix*, or
    ``None`` when the last character cannot be incremented."""
    last = ord(prefix[-1])
    if last >= 0x10FFFF:
        return None
    nxt = 0xE000 if 0xD800 <= last + 1 <= 0xDFFF else last + 1  # skip surrogates
    return prefix[:-1] + chr(nxt)


def new_trace_id() -> str:
    """A fresh opaque trace ID (``fact-`` + 32 hex chars of a uuid4)."""
    return TRACE_ID_PREFIX + uuid.uuid4().hex


class FactStore:
    """Append-only store of values with provenance, persisted under ``state_dir``."""

    def __init__(self, state_dir: Path, *, trace: TraceSink | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / FACTS_DB_FILENAME
        self._trace: TraceSink = trace if trace is not None else NullTraceSink()
        self._lock = threading.RLock()
        self._conn_obj: sqlite3.Connection | None = None
        try:
            self._ensure_schema()
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_env(cls, env: CoreEnvironment) -> FactStore:
        """The host's fact store: ``env.paths.state_dir/facts.db``, tracing to ``env.trace``."""
        return cls(env.paths.state_dir, trace=env.trace)

    # -- SQLite plumbing -----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._conn_obj is None:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=_CONNECT_TIMEOUT_S,
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                _enable_wal(conn)
            except BaseException:
                conn.close()
                raise
            self._conn_obj = conn
        return self._conn_obj

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            version = self.schema_version()
            if version > SCHEMA_VERSION:
                raise FactStoreError(
                    f"fact store {self.path} has schema version {version}, newer than "
                    f"supported {SCHEMA_VERSION}; refusing to open"
                )
            if version == SCHEMA_VERSION:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS facts (
                        trace_id TEXT PRIMARY KEY,
                        source_kind TEXT NOT NULL
                            CHECK (source_kind IN ('document', 'tool', 'user')),
                        source_ref TEXT NOT NULL,
                        span TEXT,
                        value TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS facts_source_idx "
                    "ON facts(source_kind, source_ref)"
                )
                # v2: prefix scans on source_ref alone (FactStore.query).
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS facts_ref_idx ON facts(source_ref)"
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        """The database's ``PRAGMA user_version``."""
        with self._lock:
            row = self._conn().execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row is not None else 0

    def close(self) -> None:
        """Checkpoint the WAL and release the connection. Idempotent."""
        with self._lock:
            conn, self._conn_obj = self._conn_obj, None
            if conn is None:
                return
            try:
                with suppress(sqlite3.OperationalError):
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                with suppress(sqlite3.Error):
                    conn.close()

    def __enter__(self) -> FactStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- API -----------------------------------------------------------------

    def record(
        self, source_kind: str, source_ref: str, value: Any, *, span: str | None = None,
    ) -> str:
        """Store ``value`` with its provenance; return its new trace ID."""
        if source_kind not in SOURCE_KINDS:
            raise ValueError(
                f"source_kind must be one of {sorted(SOURCE_KINDS)}, got {source_kind!r}"
            )
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError("source_ref must be a non-empty string")
        if span is not None and not isinstance(span, str):
            raise ValueError("span must be a string or None")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"fact value is not JSON-serialisable: {exc}") from exc
        trace_id = new_trace_id()
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            with conn:  # commits on success, rolls back on error
                conn.execute(
                    "INSERT INTO facts (trace_id, source_kind, source_ref, span, value, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (trace_id, source_kind, source_ref, span, encoded, created_at),
                )
        safe_emit(self._trace, {
            "event": "fact.recorded",
            "fact_id": trace_id,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "source_span": span,
        })
        return trace_id

    def resolve(self, trace_id: str) -> FactRecord | None:
        """The fact behind ``trace_id``, or ``None`` when no such fact exists."""
        if not isinstance(trace_id, str) or not trace_id:
            return None
        with self._lock:
            row = self._conn().execute(
                "SELECT trace_id, source_kind, source_ref, span, value, created_at "
                "FROM facts WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
        if row is None:
            return None
        return _record(row)

    def query(
        self,
        *,
        ref_prefix: str | None = None,
        source_kind: str | None = None,
        limit: int | None = None,
    ) -> list[FactRecord]:
        """Facts in creation order (oldest first), optionally filtered.

        ``ref_prefix`` keeps facts whose ``source_ref`` starts with it (case-sensitive,
        no wildcards); ``source_kind`` keeps one kind; ``limit`` caps the count (the
        oldest ``limit`` matches).
        """
        if ref_prefix is not None and not isinstance(ref_prefix, str):
            raise ValueError("ref_prefix must be a string or None")
        if source_kind is not None and source_kind not in SOURCE_KINDS:
            raise ValueError(
                f"source_kind must be one of {sorted(SOURCE_KINDS)}, got {source_kind!r}"
            )
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                                  or limit < 0):
            raise ValueError(f"limit must be a non-negative int or None, got {limit!r}")
        where: list[str] = []
        params: list[Any] = []
        if ref_prefix:
            upper = _prefix_upper_bound(ref_prefix)
            if upper is None:
                where.append("substr(source_ref, 1, ?) = ?")
                params += [len(ref_prefix), ref_prefix]
            else:
                # A range the facts_ref_idx index serves (BINARY collation compares
                # UTF-8 bytes, which order like code points).
                where.append("source_ref >= ? AND source_ref < ?")
                params += [ref_prefix, upper]
        if source_kind is not None:
            where.append("source_kind = ?")
            params.append(source_kind)
        sql = (
            "SELECT trace_id, source_kind, source_ref, span, value, created_at FROM facts"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY rowid"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [_record(row) for row in rows]

    def count(self) -> int:
        """Number of recorded facts."""
        with self._lock:
            return int(self._conn().execute("SELECT COUNT(*) FROM facts").fetchone()[0])


__all__ = [
    "FACTS_DB_FILENAME",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "TRACE_ID_PREFIX",
    "FactRecord",
    "FactStore",
    "FactStoreError",
    "SourceKind",
    "new_trace_id",
]
