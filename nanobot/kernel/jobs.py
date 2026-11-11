"""Durable job record (spec 015 phase 1).

One record per owner request, shared by the kernel and every wrapper. ``jobs`` holds the
latest state plus wrapper references; ``job_events`` is append-only and carries a real
timestamp, and for a failed, blocked or unknown outcome the failing *side* and a *cause*
code. Status is always read from here, never from what the agent said earlier.

The store is its own SQLite file (WAL), like the other kernel stores. Module-level imports
are stdlib + loguru only; ``nanobot.security.redact`` is imported lazily (kernel import
rule, Ruling C). The path is supplied by the caller (from ``CoreEnvironment``), never read
from the ambient process.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

JOBS_DB_NAME = "jobs.sqlite3"
_MAX_DETAIL_CHARS = 2000
_MAX_REQUEST_CHARS = 4000


class JobState(str, Enum):
    HEARD = "heard"
    NEEDS_ANSWER = "needs_answer"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class FailureSide(str, Enum):
    MOEKA = "moeka"
    WRAPPER = "wrapper"
    WONHEE = "wonhee"
    STAGECRAFT = "stagecraft"
    TELEGRAM = "telegram"
    POLICY = "policy"
    WORKER = "worker"


# States whose arrival is worth telling the owner about (spec 015 notifier).
NOTIFY_STATES = frozenset({
    JobState.NEEDS_ANSWER, JobState.BLOCKED, JobState.DONE, JobState.FAILED,
    JobState.OUTCOME_UNKNOWN,
})
NOTIFY_PENDING, NOTIFY_SENDING, NOTIFY_SENT = "pending", "sending", "sent"
NOTIFY_UNCERTAIN, NOTIFY_SKIPPED = "uncertain", "skipped"

ACTIVE_STATES = frozenset({JobState.HEARD, JobState.NEEDS_ANSWER, JobState.WORKING})
# States that must say which side stopped the job and why.
NEEDS_CAUSE = frozenset({JobState.FAILED, JobState.BLOCKED, JobState.OUTCOME_UNKNOWN})


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_id: str
    ts: str
    state: JobState
    side: FailureSide | None = None
    cause: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    session_key: str
    role: str
    request_text: str
    state: JobState
    refs: dict[str, str] = field(default_factory=dict)
    created: str = ""
    updated: str = ""

    @property
    def route(self) -> tuple[str, str, str] | None:
        """(channel, chat_id, thread_id) the owner wrote from, when recorded."""
        channel, chat = self.refs.get("route_channel"), self.refs.get("route_chat")
        if not channel or not chat:
            return None
        return channel, chat, self.refs.get("route_thread", "")


@dataclass(frozen=True, slots=True)
class Notification:
    job_id: str
    event_seq: int
    status: str
    attempts: int
    state: JobState
    ts: str


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def jobs_db_path(state_dir: Path | str) -> Path:
    """``<state_dir>/jobs.sqlite3``."""
    return Path(state_dir) / JOBS_DB_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _redact(text: str, limit: int) -> str:
    from nanobot.security.redact import redact_text

    out = redact_text(str(text or "")) or ""
    return out if len(out) <= limit else out[:limit] + "..."


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    request_text TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    ts TEXT NOT NULL,
    state TEXT NOT NULL,
    side TEXT,
    cause TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS job_notifications (
    job_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    PRIMARY KEY (job_id, event_seq)
);
CREATE TABLE IF NOT EXISTS job_links (
    parent TEXT NOT NULL,
    child TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (parent, child)
);
CREATE INDEX IF NOT EXISTS job_links_parent ON job_links(parent);
CREATE INDEX IF NOT EXISTS job_notifications_status ON job_notifications(status, ts);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, updated);
"""


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, JobState], None]] = []
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes ---------------------------------------------------------------------

    def add_listener(self, callback: Callable[[str, JobState], None]) -> None:
        """Call *callback(job_id, state)* after each committed transition (a wake signal only;
        the ``job_notifications`` table is the durable source of truth)."""
        self._listeners.append(callback)

    def _notify_listeners(self, job_id: str, state: JobState) -> None:
        for callback in list(self._listeners):
            try:
                callback(job_id, state)
            except Exception:  # a listener must never break the record
                from loguru import logger

                logger.exception("job listener failed for {}", job_id)

    def open_job(
        self, *, session_key: str, request_text: str, role: str = "", job_id: str | None = None,
        route: tuple[str, str, str] | None = None,
    ) -> Job:
        jid = job_id or new_job_id()
        now = _now()
        text = _redact(request_text, _MAX_REQUEST_CHARS)
        refs: dict[str, str] = {}
        if route and route[0] and route[1]:
            refs = {"route_channel": str(route[0]), "route_chat": str(route[1])}
            if route[2]:
                refs["route_thread"] = str(route[2])
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs(id, session_key, role, request_text, state, refs, created,"
                " updated) VALUES (?,?,?,?,?,?,?,?)",
                (jid, session_key, role, text, JobState.HEARD.value,
                 json.dumps(refs, sort_keys=True), now, now),
            )
            self._db.execute(
                "INSERT INTO job_events(job_id, ts, state) VALUES (?,?,?)",
                (jid, now, JobState.HEARD.value),
            )
            self._db.commit()
        return Job(jid, session_key, role, text, JobState.HEARD, refs, now, now)

    def transition(
        self,
        job_id: str,
        state: JobState,
        *,
        side: FailureSide | None = None,
        cause: str | None = None,
        detail: str = "",
    ) -> None:
        if state in NEEDS_CAUSE and (side is None or not cause):
            raise ValueError(f"{state.value} needs a failure side and a cause")
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET state=?, updated=? WHERE id=?", (state.value, now, job_id),
            )
            if cur.rowcount == 0:
                self._db.rollback()
                raise KeyError(job_id)
            ev = self._db.execute(
                "INSERT INTO job_events(job_id, ts, state, side, cause, detail)"
                " VALUES (?,?,?,?,?,?)",
                (job_id, now, state.value, side.value if side else None, cause,
                 _redact(detail, _MAX_DETAIL_CHARS)),
            )
            if state in NOTIFY_STATES:
                self._db.execute(
                    "INSERT INTO job_notifications(job_id, event_seq, status, ts)"
                    " VALUES (?,?,?,?)",
                    (job_id, ev.lastrowid, NOTIFY_PENDING, now),
                )
            self._db.commit()
        self._notify_listeners(job_id, state)

    def link_ref(self, job_id: str, kind: str, value: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT refs FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            refs = json.loads(row["refs"])
            refs[kind] = str(value)
            self._db.execute(
                "UPDATE jobs SET refs=?, updated=? WHERE id=?",
                (json.dumps(refs, sort_keys=True), _now(), job_id),
            )
            self._db.commit()

    # -- reads ----------------------------------------------------------------------

    def _job(self, row: sqlite3.Row) -> Job:
        return Job(
            row["id"], row["session_key"], row["role"], row["request_text"],
            JobState(row["state"]), json.loads(row["refs"]), row["created"], row["updated"],
        )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def history(self, job_id: str) -> list[JobEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY seq", (job_id,),
            ).fetchall()
        return [
            JobEvent(
                r["job_id"], r["ts"], JobState(r["state"]),
                FailureSide(r["side"]) if r["side"] else None, r["cause"], r["detail"],
            )
            for r in rows
        ]

    def active(self, *, session_key: str | None = None) -> list[Job]:
        marks = ",".join("?" for _ in ACTIVE_STATES)
        sql = f"SELECT * FROM jobs WHERE state IN ({marks})"
        args: list[Any] = [s.value for s in ACTIVE_STATES]
        if session_key is not None:
            sql += " AND session_key=?"
            args.append(session_key)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created", args).fetchall()
        return [self._job(r) for r in rows]

    def latest_waiting(self, session_key: str) -> Job | None:
        """The newest job in this session that is waiting on the owner's answer."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE session_key=? AND state=? ORDER BY updated DESC LIMIT 1",
                (session_key, JobState.NEEDS_ANSWER.value),
            ).fetchone()
        return self._job(row) if row else None

    def recent(self, limit: int = 10) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY updated DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [self._job(r) for r in rows]

    def find_by_ref(self, kind: str, value: str) -> Job | None:
        # refs are a small JSON object per job; a scan of non-terminal-first recent rows is
        # enough at this scale and avoids a second table.
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY updated DESC").fetchall()
        for r in rows:
            if json.loads(r["refs"]).get(kind) == str(value):
                return self._job(r)
        return None

    def latest_for_session(self, session_key: str) -> Job | None:
        """Newest job opened for this session (any state)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE session_key=? ORDER BY created DESC, rowid DESC LIMIT 1",
                (session_key,),
            ).fetchone()
        return self._job(row) if row else None

    def list(
        self, *, states: frozenset[JobState] | set[JobState] | None = None,
        since: str | None = None, role: str | None = None, limit: int = 100,
    ) -> list[Job]:
        """Jobs filtered by state, role and ``updated >= since`` (ISO), newest first."""
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if states:
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
            args += [s.value for s in states]
        if since:
            sql += " AND updated >= ?"
            args.append(since)
        if role is not None:
            sql += " AND role=?"
            args.append(role)
        with self._lock:
            rows = self._db.execute(
                sql + " ORDER BY updated DESC LIMIT ?", [*args, int(limit)],
            ).fetchall()
        return [self._job(r) for r in rows]

    # -- links between jobs ---------------------------------------------------------

    def link_child(self, parent: str, child: str, kind: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO job_links(parent, child, kind) VALUES (?,?,?)",
                (parent, child, kind),
            )
            self._db.commit()

    def children(self, parent: str) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT j.* FROM jobs j JOIN job_links l ON l.child=j.id WHERE l.parent=?"
                " ORDER BY j.created", (parent,),
            ).fetchall()
        return [self._job(r) for r in rows]

    def parent_of(self, child: str) -> Job | None:
        with self._lock:
            row = self._db.execute(
                "SELECT j.* FROM jobs j JOIN job_links l ON l.parent=j.id WHERE l.child=?"
                " LIMIT 1", (child,),
            ).fetchone()
        return self._job(row) if row else None

    def active_children(self, parent: str) -> list[Job]:
        return [c for c in self.children(parent) if c.state in ACTIVE_STATES]

    # -- notification outbox --------------------------------------------------------

    def pending_notifications(self, limit: int = 20) -> list[Notification]:
        """Notifications still to send, oldest first. ``sending`` rows left by a crash are
        NOT returned: they may have gone out, so they are marked uncertain on recovery."""
        with self._lock:
            rows = self._db.execute(
                "SELECT n.*, e.state AS state FROM job_notifications n"
                " JOIN job_events e ON e.seq = n.event_seq"
                " WHERE n.status=? ORDER BY n.ts, n.event_seq LIMIT ?",
                (NOTIFY_PENDING, int(limit)),
            ).fetchall()
        return [
            Notification(r["job_id"], r["event_seq"], r["status"], r["attempts"],
                         JobState(r["state"]), r["ts"])
            for r in rows
        ]

    def set_notification(self, job_id: str, event_seq: int, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE job_notifications SET status=?, attempts=attempts+?"
                " WHERE job_id=? AND event_seq=?",
                (status, 1 if status == NOTIFY_SENDING else 0, job_id, event_seq),
            )
            self._db.commit()

    def recover_notifications(self) -> int:
        """After a restart a ``sending`` row may or may not have been delivered: mark it
        uncertain rather than send twice. Returns how many were marked."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE job_notifications SET status=? WHERE status=?",
                (NOTIFY_UNCERTAIN, NOTIFY_SENDING),
            )
            self._db.commit()
        return cur.rowcount

    def notification_status(self, job_id: str) -> list[tuple[int, str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT n.event_seq, n.status, e.state FROM job_notifications n"
                " JOIN job_events e ON e.seq=n.event_seq WHERE n.job_id=? ORDER BY n.event_seq",
                (job_id,),
            ).fetchall()
        return [(r["event_seq"], r["status"], r["state"]) for r in rows]
