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
from typing import Any

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
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, updated);
"""


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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

    def open_job(
        self, *, session_key: str, request_text: str, role: str = "", job_id: str | None = None,
    ) -> Job:
        jid = job_id or new_job_id()
        now = _now()
        text = _redact(request_text, _MAX_REQUEST_CHARS)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs(id, session_key, role, request_text, state, refs, created,"
                " updated) VALUES (?,?,?,?,?,?,?,?)",
                (jid, session_key, role, text, JobState.HEARD.value, "{}", now, now),
            )
            self._db.execute(
                "INSERT INTO job_events(job_id, ts, state) VALUES (?,?,?)",
                (jid, now, JobState.HEARD.value),
            )
            self._db.commit()
        return Job(jid, session_key, role, text, JobState.HEARD, {}, now, now)

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
            self._db.execute(
                "INSERT INTO job_events(job_id, ts, state, side, cause, detail)"
                " VALUES (?,?,?,?,?,?)",
                (job_id, now, state.value, side.value if side else None, cause,
                 _redact(detail, _MAX_DETAIL_CHARS)),
            )
            self._db.commit()

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
