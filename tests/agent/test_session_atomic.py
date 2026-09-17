"""Tests for SQLite session persistence: roundtrip, atomicity, legacy import."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from nanobot.providers.base import ProviderConversationState
from nanobot.session.manager import Session, SessionManager
from nanobot.session.sqlite_store import SqliteSessionStore


def _manager(workspace: Path, *, sessions_root: Path | None = None) -> SessionManager:
    """Construct a SessionManager backed by moeka's SqliteSessionStore."""
    return SessionManager(
        workspace,
        sessions_root=sessions_root,
        store=SqliteSessionStore(workspace, sessions_root=sessions_root),
    )


class TestSqliteRoundtrip:
    def test_save_creates_sessions_db(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:1")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi", tool_calls=[{"id": "x"}])
        mgr.save(session)

        from nanobot.session.sqlite_store import get_store

        assert get_store(mgr).db_path.exists()
        fresh = _manager(tmp_path)
        loaded = fresh.get_or_create("test:1")
        assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
        assert loaded.messages[1]["tool_calls"] == [{"id": "x"}]

    def test_metadata_and_consolidation_roundtrip(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:meta")
        session.add_message("user", "a")
        session.metadata["title"] = "My chat"
        session.last_consolidated = 1
        mgr.save(session)

        loaded = _manager(tmp_path).get_or_create("test:meta")
        assert loaded.metadata["title"] == "My chat"
        assert loaded.last_consolidated == 1

    def test_save_is_full_replace(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:replace")
        session.add_message("user", "one")
        session.add_message("user", "two")
        mgr.save(session)
        session.messages = session.messages[-1:]
        mgr.save(session)

        loaded = _manager(tmp_path).get_or_create("test:replace")
        assert len(loaded.messages) == 1
        assert loaded.messages[0]["content"] == "two"

    def test_unicode_content_roundtrip(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:uni")
        session.add_message("user", "héllo 日本語 🦊")
        mgr.save(session)
        loaded = _manager(tmp_path).get_or_create("test:uni")
        assert loaded.messages[0]["content"] == "héllo 日本語 🦊"

    def test_corrupt_message_row_skipped(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:corrupt")
        session.add_message("user", "good")
        mgr.save(session)
        from nanobot.session.sqlite_store import get_store

        conn = sqlite3.connect(get_store(mgr).db_path)
        conn.execute(
            "INSERT INTO messages(session_key, seq, role, created_at, data)"
            " VALUES ('test:corrupt', 99, 'user', NULL, '{not json')"
        )
        conn.commit()
        conn.close()

        loaded = _manager(tmp_path).get_or_create("test:corrupt")
        assert [m["content"] for m in loaded.messages] == ["good"]

    def test_out_of_range_last_consolidated_resets_to_zero(self, tmp_path: Path):
        """An out-of-range offset (corrupt row) would hide all history; reset it (#4066)."""
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:badconsolidated")
        session.add_message("user", "hi")
        mgr.save(session)
        from nanobot.session.sqlite_store import get_store

        conn = sqlite3.connect(get_store(mgr).db_path)
        conn.execute(
            "UPDATE sessions SET last_consolidated = 5 WHERE key = 'test:badconsolidated'"
        )
        conn.commit()
        conn.close()

        loaded = _manager(tmp_path)._load("test:badconsolidated")
        assert loaded is not None
        # offset 5 exceeds the single loaded message; reset to avoid hiding history.
        assert loaded.last_consolidated == 0

    def test_provider_state_round_trips_in_private_record_only(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        secret = "encrypted-reasoning-blob"
        session = Session(
            key="test:provider-state",
            provider_state=ProviderConversationState(
                kind="openai_responses",
                provider="openai:https://api.openai.com/v1",
                model="gpt-5.6",
                version=1,
                payload={
                    "items": [
                        {
                            "type": "reasoning",
                            "encrypted_content": secret,
                        }
                    ]
                },
                pending_messages=[{"role": "user", "content": "continue"}],
            ),
        )
        session.add_message("user", "hello")
        mgr.save(session)

        # provider_state rides along in the sessions.metadata column under a
        # reserved key rather than a dedicated jsonl-style sidecar record.
        from nanobot.session.sqlite_store import get_store

        row = get_store(mgr)._conn().execute(
            "SELECT metadata FROM sessions WHERE key = ?", (session.key,)
        ).fetchone()
        stored_metadata = json.loads(row[0])
        assert secret in stored_metadata["_provider_state"]["payload"]["items"][0][
            "encrypted_content"
        ]
        message_rows = get_store(mgr)._conn().execute(
            "SELECT data FROM messages WHERE session_key = ?", (session.key,)
        ).fetchall()
        assert secret not in "".join(data for (data,) in message_rows)

        mgr.invalidate(session.key)
        loaded = mgr.get_or_create(session.key)
        assert loaded.provider_state is not None
        assert loaded.provider_state.to_private_record() == session.provider_state.to_private_record()

        public_payload = mgr.read_session_file(session.key)
        assert public_payload is not None
        assert public_payload["messages"] == [session.messages[0]]
        assert secret not in json.dumps(public_payload)
        assert secret not in json.dumps(mgr.list_sessions())

    def test_provider_state_does_not_consume_list_preview_budget(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        import nanobot.session.sqlite_store as sqlite_store

        monkeypatch.setattr(sqlite_store, "_SESSION_LIST_PREVIEW_MAX_CHARS", 100)
        mgr = _manager(tmp_path)
        session = Session(
            key="test:provider-state-preview",
            provider_state=ProviderConversationState(
                kind="openai_responses",
                provider="openai:test",
                model="test-model",
                version=1,
                payload={"items": [{"encrypted_content": "x" * 200}]},
            ),
        )
        session.add_message("user", "visible preview")
        mgr.save(session)

        assert mgr.list_sessions()[0]["preview"] == "visible preview"

    def test_clear_and_fork_discard_provider_state(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        state = ProviderConversationState(
            kind="openai_responses",
            provider="openai:test",
            model="gpt-5.6",
            version=1,
            payload={"items": []},
        )
        source = Session(key="test:state-source", provider_state=state)
        source.add_message("user", "hello")
        mgr.save(source)

        fork = mgr.fork_session_before_user_index(
            source.key,
            "test:state-fork",
            1,
        )
        assert fork is not None
        assert fork.provider_state is None

        source.clear()
        assert source.provider_state is None

    def test_invalid_provider_state_record_is_not_public_history(self, tmp_path: Path):
        """A malformed ``_provider_state`` blob (missing required fields) must be
        dropped by ``from_private_record`` rather than surfacing as a message."""
        mgr = _manager(tmp_path)
        key = "test:bad-provider-state"
        now = datetime.now().isoformat()
        from nanobot.session.sqlite_store import get_store

        conn = get_store(mgr)._conn()
        with conn:
            conn.execute(
                "INSERT INTO sessions(key, created_at, updated_at, metadata,"
                " last_consolidated) VALUES (?, ?, ?, ?, 0)",
                (
                    key,
                    now,
                    now,
                    json.dumps({"_provider_state": {"kind": "openai_responses"}}),
                ),
            )
            conn.execute(
                "INSERT INTO messages(session_key, seq, role, created_at, data)"
                " VALUES (?, 0, 'user', ?, ?)",
                (key, now, json.dumps({"role": "user", "content": "safe"})),
            )

        loaded = mgr._load(key)
        assert loaded is not None
        assert loaded.provider_state is None
        assert loaded.messages == [{"role": "user", "content": "safe"}]


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
        mgr = _manager(tmp_path)
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
        mgr = _manager(tmp_path)
        loaded = mgr.get_or_create("test:trunc")
        assert [m["content"] for m in loaded.messages] == ["kept", "also kept"]

    def test_newer_jsonl_wins_over_stale_db(self, tmp_path: Path):
        """A jsonl written after the db row (old-code process during the
        migration window) replaces the stale db copy — no message loss."""
        mgr = _manager(tmp_path)
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
        fresh = _manager(tmp_path)
        loaded = fresh.get_or_create("test:overlap")
        assert loaded.messages[0]["content"] == "fresher jsonl version"

    def test_db_session_wins_over_jsonl(self, tmp_path: Path):
        mgr = _manager(tmp_path)
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
        fresh = _manager(tmp_path)
        loaded = fresh.get_or_create("test:dup")
        assert loaded.messages[0]["content"] == "db version"

    def test_all_corrupt_file_not_imported(self, tmp_path: Path):
        path = self._write_jsonl(tmp_path, "test:allbad", [
            "not json", "{broken", "[1,2,",
        ])
        mgr = _manager(tmp_path)
        assert mgr.read_session_file("test:allbad") is None
        # File still renamed so it isn't re-parsed every startup.
        assert not path.exists()

    def test_dump_jsonl_export(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        session = mgr.get_or_create("test:dump")
        session.add_message("user", "exported")
        mgr.save(session)
        from nanobot.session.sqlite_store import get_store

        dump = get_store(mgr).dump_jsonl("test:dump")
        assert dump is not None
        lines = [json.loads(line) for line in dump.strip().splitlines()]
        assert lines[0]["_type"] == "metadata"
        assert lines[1]["content"] == "exported"
        assert get_store(mgr).dump_jsonl("missing:key") is None


class TestListSessions:
    def test_list_sessions_orders_and_previews(self, tmp_path: Path):
        mgr = _manager(tmp_path)
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
        mgr = _manager(tmp_path)
        s = mgr.get_or_create("c:3")
        s.add_message("assistant", "proactive hello")
        mgr.save(s)
        (info,) = mgr.list_sessions()
        assert info["preview"] == "proactive hello"
