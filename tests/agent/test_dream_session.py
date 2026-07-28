"""Tests for Dream session key generation and rotation."""

from datetime import datetime, timedelta
from unittest.mock import patch

from nanobot.agent.memory import MemoryStore
from nanobot.session.manager import SessionManager


class TestDreamSessionKey:
    def test_contains_timestamp(self):
        key = MemoryStore.dream_session_key()
        assert key.startswith("dream:")
        ts_part = key.split(":", 1)[1]
        datetime.strptime(ts_part, "%Y%m%d-%H%M%S")

    def test_unique_across_calls(self):
        now = datetime(2026, 5, 28, 10, 0, 0)
        with patch("nanobot.agent.memory.datetime") as mock_dt:
            mock_dt.now.side_effect = [now, now + timedelta(seconds=1)]
            k1 = MemoryStore.dream_session_key()
            k2 = MemoryStore.dream_session_key()

        assert k1 != k2


class TestPruneDreamSessions:
    @staticmethod
    def _make_dream_session(sessions: SessionManager, key: str, updated_at: datetime) -> None:
        session = sessions.get_or_create(key)
        session.add_message("user", "dream scratch turn")
        session.updated_at = updated_at
        sessions.save(session)

    def test_keeps_n_most_recent(self, tmp_path):
        sessions = SessionManager(tmp_path)
        base = datetime(2026, 5, 28, 10, 0, 0)
        keys = [f"dream:20260528-{100000 + i:06d}" for i in range(15)]
        for i, key in enumerate(keys):
            self._make_dream_session(sessions, key, base + timedelta(seconds=i))

        self._make_dream_session(sessions, "telegram:123", base)

        MemoryStore.prune_dream_sessions(sessions, keep=10)

        remaining = {row["key"] for row in sessions.list_sessions()}
        # Oldest 5 dream sessions pruned, most recent 10 kept.
        assert remaining == set(keys[5:]) | {"telegram:123"}

    def test_noop_when_under_limit(self, tmp_path):
        sessions = SessionManager(tmp_path)
        base = datetime(2026, 5, 28, 10, 0, 0)
        keys = [f"dream:20260528-{100000 + i:06d}" for i in range(3)]
        for i, key in enumerate(keys):
            self._make_dream_session(sessions, key, base + timedelta(seconds=i))

        MemoryStore.prune_dream_sessions(sessions, keep=10)
        assert {row["key"] for row in sessions.list_sessions()} == set(keys)

    def test_empty_store_noop(self, tmp_path):
        sessions = SessionManager(tmp_path)
        MemoryStore.prune_dream_sessions(sessions, keep=10)
        assert sessions.list_sessions() == []
