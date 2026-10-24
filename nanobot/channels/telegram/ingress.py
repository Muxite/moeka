"""SQLite receipts retain Telegram updates until their downstream handoff is durable."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class IngressReceipts:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS receipt (
                    update_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL
                );
            """)

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def save(self, update_id: int, kind: str, payload: str) -> bool:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO receipt(update_id,kind,payload,created) VALUES(?,?,?,?)",
                       (update_id, kind, payload, time.time()))
            return db.execute("SELECT state FROM receipt WHERE update_id=?", (update_id,)).fetchone() == ("pending",)

    def complete(self, update_ids: list[int]) -> None:
        with self._db() as db:
            db.executemany("UPDATE receipt SET state='done' WHERE update_id=?", [(v,) for v in update_ids])

    def pending(self) -> list[tuple[int, str, str]]:
        with self._db() as db:
            return list(db.execute("SELECT update_id,kind,payload FROM receipt WHERE state='pending' ORDER BY update_id"))
