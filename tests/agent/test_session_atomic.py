"""Tests for SQLite session persistence: roundtrip, atomicity, legacy import."""

import json
import sqlite3
from pathlib import Path

from nanobot.session.manager import SessionManager


class TestSqliteRoundtrip:
    def test_save_creates_sessions_db(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:1")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi", tool_calls=[{"id": "x"}])
        mgr.save(session)

        assert (tmp_path / "sessions.db").exists()
        fresh = SessionManager(tmp_path)
        loaded = fresh.get_or_create("test:1")
        assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
        assert loaded.messages[1]["tool_calls"] == [{"id": "x"}]

    def test_metadata_and_consolidation_roundtrip(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:meta")
        session.add_message("user", "a")
        session.metadata["title"] = "My chat"
        session.last_consolidated = 1
        mgr.save(session)

        loaded = SessionManager(tmp_path).get_or_create("test:meta")
        assert loaded.metadata["title"] == "My chat"
        assert loaded.last_consolidated == 1

    def test_save_is_full_replace(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:replace")
        session.add_message("user", "one")
        session.add_message("user", "two")
        mgr.save(session)
        session.messages = session.messages[-1:]
        mgr.save(session)

        loaded = SessionManager(tmp_path).get_or_create("test:replace")
        assert len(loaded.messages) == 1
        assert loaded.messages[0]["content"] == "two"

    def test_unicode_content_roundtrip(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:uni")
        session.add_message("user", "héllo 日本語 🦊")
        mgr.save(session)
        loaded = SessionManager(tmp_path).get_or_create("test:uni")
        assert loaded.messages[0]["content"] == "héllo 日本語 🦊"

    def test_corrupt_message_row_skipped(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:corrupt")
        session.add_message("user", "good")
        mgr.save(session)
        conn = sqlite3.connect(tmp_path / "sessions.db")
        conn.execute(
            "INSERT INTO messages(session_key, seq, role, created_at, data)"
            " VALUES ('test:corrupt', 99, 'user', NULL, '{not json')"
        )
        conn.commit()
        conn.close()

        loaded = SessionManager(tmp_path).get_or_create("test:corrupt")
        assert [m["content"] for m in loaded.messages] == ["good"]


class TestLegacyJsonlImport:
    @staticmethod
    def _write_jsonl(workspace: Path, key: str, lines: list[str]) -> Path:
        sessions = workspace / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"{SessionManager.safe_key(key)}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_jsonl_imported_once_and_renamed(self, tmp_path: Path):
        path = self._write_jsonl(tmp_path, "telegram:42", [
            json.dumps({"_type": "metadata", "key": "telegram:42",
                        "created_at": "2026-01-01T00:00:00",
                        "updated_at": "2026-01-02T00:00:00",
                        "metadata": {"title": "imported"},
                        "last_consolidated": 0}),
            json.dumps({"role": "user", "content": "from jsonl"}),
        ])
        mgr = SessionManager(tmp_path)
        loaded = mgr.get_or_create("telegram:42")
        assert loaded.messages[0]["content"] == "from jsonl"
        assert loaded.metadata["title"] == "imported"
        assert not path.exists()
        assert path.with_suffix(".jsonl.imported").exists()

    def test_corrupt_lines_skipped_on_import(self, tmp_path: Path):
        self._write_jsonl(tmp_path, "test:trunc", [
            json.dumps({"_type": "metadata", "key": "test:trunc",
                        "created_at": "2026-01-01T00:00:00",
                        "updated_at": "2026-01-01T00:00:00",
                        "metadata": {}, "last_consolidated": 0}),
            json.dumps({"role": "user", "content": "kept"}),
            '{"role": "assistant", "content": "trunca',  # corrupt
            json.dumps({"role": "assistant", "content": "also kept"}),
        ])
        mgr = SessionManager(tmp_path)
        loaded = mgr.get_or_create("test:trunc")
        assert [m["content"] for m in loaded.messages] == ["kept", "also kept"]

    def test_newer_jsonl_wins_over_stale_db(self, tmp_path: Path):
        """A jsonl written after the db row (old-code process during the
        migration window) replaces the stale db copy — no message loss."""
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:overlap")
        session.add_message("user", "stale db version")
        mgr.save(session)

        self._write_jsonl(tmp_path, "test:overlap", [
            json.dumps({"_type": "metadata", "key": "test:overlap",
                        "created_at": "2026-01-01T00:00:00",
                        "updated_at": "2099-01-01T00:00:00",
                        "metadata": {}, "last_consolidated": 0}),
            json.dumps({"role": "user", "content": "fresher jsonl version"}),
        ])
        fresh = SessionManager(tmp_path)
        loaded = fresh.get_or_create("test:overlap")
        assert loaded.messages[0]["content"] == "fresher jsonl version"

    def test_db_session_wins_over_jsonl(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:dup")
        session.add_message("user", "db version")
        mgr.save(session)

        self._write_jsonl(tmp_path, "test:dup", [
            json.dumps({"_type": "metadata", "key": "test:dup",
                        "created_at": "2026-01-01T00:00:00",
                        "updated_at": "2026-01-01T00:00:00",
                        "metadata": {}, "last_consolidated": 0}),
            json.dumps({"role": "user", "content": "jsonl version"}),
        ])
        fresh = SessionManager(tmp_path)
        loaded = fresh.get_or_create("test:dup")
        assert loaded.messages[0]["content"] == "db version"

    def test_all_corrupt_file_not_imported(self, tmp_path: Path):
        path = self._write_jsonl(tmp_path, "test:allbad", [
            "not json", "{broken", "[1,2,",
        ])
        mgr = SessionManager(tmp_path)
        assert mgr.read_session_file("test:allbad") is None
        # File still renamed so it isn't re-parsed every startup.
        assert not path.exists()

    def test_dump_jsonl_export(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("test:dump")
        session.add_message("user", "exported")
        mgr.save(session)
        dump = mgr.dump_jsonl("test:dump")
        assert dump is not None
        lines = [json.loads(line) for line in dump.strip().splitlines()]
        assert lines[0]["_type"] == "metadata"
        assert lines[1]["content"] == "exported"
        assert mgr.dump_jsonl("missing:key") is None


class TestListSessions:
    def test_list_sessions_orders_and_previews(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        for key, text in (("a:1", "first chat"), ("b:2", "second chat")):
            s = mgr.get_or_create(key)
            s.add_message("user", text)
            mgr.save(s)
        infos = mgr.list_sessions()
        assert {i["key"] for i in infos} == {"a:1", "b:2"}
        by_key = {i["key"]: i for i in infos}
        assert by_key["a:1"]["preview"] == "first chat"
        assert by_key["b:2"]["preview"] == "second chat"

    def test_assistant_preview_fallback(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        s = mgr.get_or_create("c:3")
        s.add_message("assistant", "proactive hello")
        mgr.save(s)
        (info,) = mgr.list_sessions()
        assert info["preview"] == "proactive hello"
