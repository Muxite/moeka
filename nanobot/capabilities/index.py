"""The SQLite capability index (spec 011 FR-004, FR-007).

One row per capability plus one row per required input, any-of input, output and
effect, and a ``(type, ancestor)`` closure table built from the vocabulary. Each row
also carries its provenance (spec 012 FR-007): ``cert_id`` (or null) and
``cert_status`` (``certified``, ``uncertified``, ``refused:<reason>`` or ``builtin``).
:meth:`CapabilityIndex.sync` makes the index equal to a full list of entries while
touching only the rows that changed; :meth:`CapabilityIndex.filter` is the
deterministic type/effect filter, evaluated in SQL.

The database is a rebuildable cache: a schema version mismatch drops and recreates it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from nanobot.capabilities.manifest import IOSpec, parse_io
from nanobot.capabilities.types import ancestors, closure

CapabilityKind = Literal["skill", "tool"]
KINDS: tuple[str, ...] = ("skill", "tool")
_SCHEMA_VERSION = 2
# One lock per database file: loops in one process that share a file serialise their
# sync-then-filter sequences, so one loop never reads another's half-synced rows.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class CapabilityEntry:
    """One capability as the index stores it. ``io=None`` = untyped.

    ``io`` may be given as a raw manifest dict; it is normalised with ``parse_io``.
    ``cert_id`` / ``cert_status`` are the provenance columns (spec 012 FR-007); an entry
    built without them is a ``builtin`` capability with no certificate.
    """

    kind: str
    name: str
    description: str
    io: IOSpec | None
    content_sha256: str
    available: bool = True
    cert_id: str | None = None
    cert_status: str = "builtin"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"capability kind must be one of {KINDS}, got {self.kind!r}")
        if self.io is not None and not isinstance(self.io, IOSpec):
            object.__setattr__(self, "io", parse_io(self.io))
        object.__setattr__(self, "available", bool(self.available))

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)


@dataclass(frozen=True)
class SyncResult:
    """Row counts of one :meth:`CapabilityIndex.sync`."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class IndexedCapability:
    """A filter result row: what the ranker and the selector need."""

    kind: str
    name: str
    description: str
    content_sha256: str
    ordinal: int
    typed: bool = field(default=False, compare=False)
    cert_id: str | None = field(default=None, compare=False)
    cert_status: str = field(default="builtin", compare=False)

    @property
    def certified(self) -> bool:
        return self.cert_status == "certified"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    io_json TEXT,
    typed INTEGER NOT NULL,
    available INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    cert_id TEXT,
    cert_status TEXT NOT NULL DEFAULT 'builtin',
    UNIQUE (kind, name)
);
CREATE TABLE IF NOT EXISTS cap_inputs_all (
    cap_id INTEGER NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cap_inputs_any (
    cap_id INTEGER NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cap_outputs (
    cap_id INTEGER NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cap_effects (
    cap_id INTEGER NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    effect TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS type_closure (
    type TEXT NOT NULL,
    ancestor TEXT NOT NULL,
    PRIMARY KEY (type, ancestor)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cap_inputs_all_cap ON cap_inputs_all(cap_id);
CREATE INDEX IF NOT EXISTS cap_inputs_any_cap ON cap_inputs_any(cap_id);
CREATE INDEX IF NOT EXISTS cap_outputs_cap ON cap_outputs(cap_id);
CREATE INDEX IF NOT EXISTS cap_effects_cap ON cap_effects(cap_id);
CREATE INDEX IF NOT EXISTS type_closure_ancestor ON type_closure(ancestor, type);
"""

_CHILD_TABLES = ("cap_inputs_all", "cap_inputs_any", "cap_outputs", "cap_effects")

# FR-007 rules 1-4, set-based: every subquery is uncorrelated, so SQLite evaluates each
# once per query (not once per capability). Parameters: kinds (JSON list or NULL), the
# allowed effects (JSON list), exec allowed (0/1), the task types' ancestor set (JSON
# list) and the requested output (or NULL).
_FILTER_SQL = """
SELECT c.kind, c.name, c.description, c.content_sha256, c.ordinal, c.typed,
       c.cert_id, c.cert_status
FROM capabilities c
WHERE (c.available = 1 OR c.kind = 'skill')
  AND (:kinds IS NULL OR c.kind IN (SELECT value FROM json_each(:kinds)))
  AND c.id NOT IN (
      SELECT cap_id FROM cap_effects
      WHERE effect NOT IN (SELECT value FROM json_each(:effects))
         OR (effect = 'sudo' AND :exec_ok = 0)
  )
  AND (c.typed = 0 OR (
      c.id NOT IN (
          SELECT cap_id FROM cap_inputs_all
          WHERE type NOT IN (SELECT value FROM json_each(:task))
      )
      AND (
          c.id NOT IN (SELECT cap_id FROM cap_inputs_any)
          OR c.id IN (
              SELECT cap_id FROM cap_inputs_any
              WHERE type IN (SELECT value FROM json_each(:task))
          )
      )
      AND (
          :requested IS NULL
          OR c.id IN (
              SELECT cap_id FROM cap_outputs
              WHERE type = 'any'
                 OR type IN (SELECT ancestor FROM type_closure WHERE type = :requested)
                 OR type IN (SELECT type FROM type_closure WHERE ancestor = :requested)
          )
      )
  ))
"""


def _lock_for(path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = _PATH_LOCKS[path] = threading.RLock()
        return lock


def _io_json(io: IOSpec | None) -> str | None:
    return None if io is None else json.dumps(io.to_dict(), sort_keys=True)


def _row_value(entry: CapabilityEntry, ordinal: int) -> tuple[Any, ...]:
    """The row's mutable columns; ``ordinal`` is last (an ordinal-only change is silent)."""
    return (
        entry.description, _io_json(entry.io), int(entry.io is not None),
        int(entry.available), entry.content_sha256, entry.cert_id, entry.cert_status, ordinal,
    )


class CapabilityIndex:
    """A SQLite (WAL) index of capabilities. ``db_path=":memory:"`` keeps it in memory.

    The default on-disk location is ``<state data_dir>/capabilities.db`` (never inside
    ``<workspace>/skills/``); the caller picks the path.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        in_memory = self.db_path == ":memory:"
        if not in_memory:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock() if in_memory else _lock_for(
            str(Path(self.db_path).resolve())
        )
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        with self._lock:
            if not in_memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._ensure_schema()

    @property
    def lock(self) -> threading.RLock:
        """The (per-file) lock: hold it to make a sync-then-filter sequence atomic."""
        return self._lock

    # -- schema --------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        conn = self._conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, _SCHEMA_VERSION):
            for table in (*_CHILD_TABLES, "capabilities", "type_closure", "meta"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(_SCHEMA)
        pairs = closure()
        stored = conn.execute("SELECT type, ancestor FROM type_closure").fetchall()
        if set(stored) != set(pairs):
            # The vocabulary changed (or a fresh db): rebuild the closure table.
            conn.execute("DELETE FROM type_closure")
            conn.executemany("INSERT INTO type_closure(type, ancestor) VALUES (?, ?)", pairs)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()

    # -- writes --------------------------------------------------------------------

    def sync(self, entries: Iterable[CapabilityEntry]) -> SyncResult:
        """Make the index equal to *entries* (the full current list).

        Inserts new capabilities, updates changed ones, deletes missing ones and leaves
        unchanged rows untouched. A later duplicate ``(kind, name)`` replaces an earlier
        one; list order is kept as each capability's ordinal ("today's order"), and an
        ordinal-only change is not counted as an update.
        """
        wanted: dict[tuple[str, str], CapabilityEntry] = {}
        for entry in entries:
            wanted[entry.key] = entry
        ordinals = {key: position for position, key in enumerate(wanted)}
        added = updated = removed = unchanged = 0
        with self._lock:
            conn = self._conn
            try:
                existing = {
                    (row[1], row[2]): row
                    for row in conn.execute(
                        "SELECT id, kind, name, description, io_json, typed, available, "
                        "content_sha256, cert_id, cert_status, ordinal FROM capabilities"
                    )
                }
                for key, row in existing.items():
                    if key not in wanted:
                        conn.execute("DELETE FROM capabilities WHERE id = ?", (row[0],))
                        removed += 1
                for key, entry in wanted.items():
                    value = _row_value(entry, ordinals[key])
                    row = existing.get(key)
                    if row is None:
                        cursor = conn.execute(
                            "INSERT INTO capabilities(kind, name, description, io_json, typed, "
                            "available, content_sha256, cert_id, cert_status, ordinal) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (entry.kind, entry.name, *value),
                        )
                        self._insert_children(int(cursor.lastrowid or 0), entry.io)
                        added += 1
                    elif tuple(row[3:10]) != value[:7]:
                        conn.execute(
                            "UPDATE capabilities SET description = ?, io_json = ?, typed = ?, "
                            "available = ?, content_sha256 = ?, cert_id = ?, cert_status = ?, "
                            "ordinal = ? WHERE id = ?",
                            (*value, row[0]),
                        )
                        for table in _CHILD_TABLES:
                            conn.execute(f"DELETE FROM {table} WHERE cap_id = ?", (row[0],))
                        self._insert_children(row[0], entry.io)
                        updated += 1
                    else:
                        if row[10] != value[7]:
                            conn.execute(
                                "UPDATE capabilities SET ordinal = ? WHERE id = ?",
                                (value[7], row[0]),
                            )
                        unchanged += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return SyncResult(added=added, updated=updated, removed=removed, unchanged=unchanged)

    def _insert_children(self, cap_id: int, io: IOSpec | None) -> None:
        if io is None:
            return
        for table, column, values in (
            ("cap_inputs_all", "type", io.inputs_all),
            ("cap_inputs_any", "type", io.inputs_any),
            ("cap_outputs", "type", io.outputs),
            ("cap_effects", "effect", io.effects),
        ):
            self._conn.executemany(
                f"INSERT INTO {table}(cap_id, {column}) VALUES (?, ?)",
                [(cap_id, value) for value in values],
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -- reads ---------------------------------------------------------------------

    def entries(self) -> list[CapabilityEntry]:
        """Every indexed capability, in ordinal (sync list) order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, name, description, io_json, available, content_sha256, "
                "cert_id, cert_status FROM capabilities ORDER BY ordinal, kind, name"
            ).fetchall()
        return [
            CapabilityEntry(
                kind=kind, name=name, description=description,
                io=parse_io(json.loads(io_json)) if io_json is not None else None,
                content_sha256=sha, available=bool(available),
                cert_id=cert_id, cert_status=cert_status,
            )
            for kind, name, description, io_json, available, sha, cert_id, cert_status in rows
        ]

    def child_rows(self) -> dict[str, list[tuple[str, str, str]]]:
        """``table -> sorted (kind, name, value)`` rows of the per-type/effect tables."""
        out: dict[str, list[tuple[str, str, str]]] = {}
        with self._lock:
            for table in _CHILD_TABLES:
                column = "effect" if table == "cap_effects" else "type"
                out[table] = sorted(
                    self._conn.execute(
                        f"SELECT c.kind, c.name, t.{column} FROM {table} t "
                        "JOIN capabilities c ON c.id = t.cap_id"
                    ).fetchall()
                )
        return out

    def __iter__(self) -> Iterator[CapabilityEntry]:
        return iter(self.entries())

    def __len__(self) -> int:
        return self.count()

    def count(self, kinds: Iterable[str] | None = None) -> int:
        """How many capabilities are indexed (of *kinds*, when given)."""
        with self._lock:
            if kinds is None:
                return int(self._conn.execute("SELECT count(*) FROM capabilities").fetchone()[0])
            return int(self._conn.execute(
                "SELECT count(*) FROM capabilities WHERE kind IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(set(kinds))),),
            ).fetchone()[0])

    def in_order(self, kinds: Iterable[str] | None = None) -> list[IndexedCapability]:
        """Every capability (of *kinds*) in ordinal order: today's full listing."""
        sql = (
            "SELECT kind, name, description, content_sha256, ordinal, typed, cert_id, "
            "cert_status FROM capabilities"
            " WHERE (:kinds IS NULL OR kind IN (SELECT value FROM json_each(:kinds)))"
            " ORDER BY ordinal, kind, name"
        )
        with self._lock:
            rows = self._conn.execute(sql, {"kinds": _kinds_json(kinds)}).fetchall()
        return [_indexed(row) for row in rows]

    def filter(
        self,
        task_types: Iterable[str],
        requested_output: str | None,
        allowed_effects: Iterable[str],
        *,
        kinds: Iterable[str] | None = None,
    ) -> list[IndexedCapability]:
        """Capabilities passing FR-007 rules 1-4, in ordinal order.

        *task_types* and *requested_output* must be vocabulary types
        (``UnknownTypeError`` otherwise). The task types are expanded to their ancestor
        set here, so "``required`` is satisfied by some task type" is set membership.
        """
        expanded = sorted({anc for task in task_types for anc in ancestors(task)})
        if requested_output is not None:
            ancestors(requested_output)  # validates
        effects = sorted(set(allowed_effects))
        params = {
            "kinds": _kinds_json(kinds),
            "effects": json.dumps(effects),
            "exec_ok": int("exec" in effects),
            "task": json.dumps(expanded),
            "requested": requested_output,
        }
        with self._lock:
            rows = self._conn.execute(
                _FILTER_SQL + " ORDER BY c.ordinal, c.kind, c.name", params,
            ).fetchall()
        return [_indexed(row) for row in rows]

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> CapabilityIndex:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _kinds_json(kinds: Iterable[str] | None) -> str | None:
    return None if kinds is None else json.dumps(sorted(set(kinds)))


def _indexed(row: tuple[Any, ...]) -> IndexedCapability:
    kind, name, description, sha, ordinal, typed, cert_id, cert_status = row
    return IndexedCapability(
        kind=kind, name=name, description=description, content_sha256=sha,
        ordinal=int(ordinal), typed=bool(typed), cert_id=cert_id,
        cert_status=cert_status or "builtin",
    )


__all__ = [
    "KINDS",
    "CapabilityEntry",
    "CapabilityIndex",
    "IndexedCapability",
    "SyncResult",
]
