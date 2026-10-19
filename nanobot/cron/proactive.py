"""Durable quiet-hour and daily-rate gate for unsolicited check-ins."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class UnsolicitedDeliveryLimit:
    """Reserve at most N unsolicited deliveries per local day inside quiet hours."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS delivery_days (
                timezone TEXT NOT NULL, local_date TEXT NOT NULL, sent INTEGER NOT NULL,
                PRIMARY KEY(timezone, local_date)
            )""")

    def reserve(
        self,
        *,
        timezone: str,
        max_per_day: int = 1,
        start_hour: int = 9,
        end_hour: int = 21,
        now: datetime | None = None,
    ) -> bool:
        if max_per_day <= 0:
            return False
        zone = ZoneInfo(timezone)
        current = now or datetime.now(zone)
        local_now = current.replace(tzinfo=zone) if current.tzinfo is None else current.astimezone(zone)
        if not start_hour <= local_now.hour < end_hour:
            return False
        local_date = local_now.date().isoformat()
        with sqlite3.connect(self.path, timeout=10, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sent FROM delivery_days WHERE timezone=? AND local_date=?",
                (timezone, local_date),
            ).fetchone()
            sent = int(row[0]) if row else 0
            if sent >= max_per_day:
                db.rollback()
                return False
            db.execute(
                "INSERT INTO delivery_days(timezone,local_date,sent) VALUES(?,?,1) "
                "ON CONFLICT(timezone,local_date) DO UPDATE SET sent=delivery_days.sent+1",
                (timezone, local_date),
            )
            db.commit()
            return True
