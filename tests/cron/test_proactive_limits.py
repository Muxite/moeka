from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nanobot.cron.proactive import UnsolicitedDeliveryLimit


def test_unsolicited_delivery_obeys_local_window_and_persists_daily_cap(tmp_path: Path) -> None:
    path = tmp_path / "cron" / "unsolicited.sqlite3"
    limiter = UnsolicitedDeliveryLimit(path)
    before = datetime.fromisoformat("2026-10-05T08:59:00-07:00")
    morning = datetime.fromisoformat("2026-10-05T09:00:00-07:00")
    evening = datetime.fromisoformat("2026-10-05T20:59:00-07:00")
    after = datetime.fromisoformat("2026-10-05T21:00:00-07:00")

    assert limiter.reserve(timezone="America/Los_Angeles", now=before) is False
    assert limiter.reserve(timezone="America/Los_Angeles", now=morning) is True
    restarted = UnsolicitedDeliveryLimit(path)
    assert restarted.reserve(timezone="America/Los_Angeles", now=evening) is False
    assert restarted.reserve(timezone="America/Los_Angeles", now=after) is False
    assert restarted.reserve(
        timezone="America/Los_Angeles", now=datetime.fromisoformat("2026-10-06T09:00:00-07:00"),
    ) is True


def test_max_zero_disables_unprompted_messages(tmp_path: Path) -> None:
    limiter = UnsolicitedDeliveryLimit(tmp_path / "cron" / "unsolicited.sqlite3")
    assert limiter.reserve(
        timezone="UTC", max_per_day=0, now=datetime.fromisoformat("2026-10-05T12:00:00+00:00"),
    ) is False


def test_configured_daily_cap_counts_multiple_reservations(tmp_path: Path) -> None:
    limiter = UnsolicitedDeliveryLimit(tmp_path / "cron" / "unsolicited.sqlite3")
    now = datetime.fromisoformat("2026-10-05T12:00:00+00:00")
    assert limiter.reserve(timezone="UTC", max_per_day=2, now=now) is True
    assert limiter.reserve(timezone="UTC", max_per_day=2, now=now) is True
    assert limiter.reserve(timezone="UTC", max_per_day=2, now=now) is False
