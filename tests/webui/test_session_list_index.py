from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

import nanobot.webui.session_list_index as session_list_index
from nanobot.cron.session_turns import CRON_HISTORY_META
from nanobot.providers.base import ProviderConversationState
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY
from nanobot.session.automation_turns import AUTOMATION_HISTORY_META
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.session.manager import SessionManager
from nanobot.session.model_selection import SESSION_MODEL_PRESET_METADATA_KEY
from nanobot.session.sqlite_store import SqliteSessionStore, get_store


def _manager(workspace: Path) -> SessionManager:
    """Construct a SessionManager backed by moeka's SqliteSessionStore."""
    return SessionManager(workspace, store=SqliteSessionStore(workspace))


def test_webui_session_list_reuses_valid_index_without_scanning_files(
    tmp_path: Path,
) -> None:
    """No file-scan cache is needed for the SQLite store — listing is always
    a single cheap query, so this just checks repeated calls stay correct."""
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:indexed")
    session.metadata[SESSION_MODEL_PRESET_METADATA_KEY] = "fast"
    session.add_message("user", "indexed preview")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "indexed preview"
    assert list_webui_sessions(manager)[0]["model_preset"] == "fast"

    rows = list_webui_sessions(manager)

    assert rows[0]["key"] == "websocket:indexed"
    assert rows[0]["preview"] == "indexed preview"
    assert rows[0]["model_preset"] == "fast"


def test_webui_session_list_indexes_workspace_scope_and_preserves_null(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    scoped = manager.get_or_create("websocket:scoped")
    scoped.metadata[WORKSPACE_SCOPE_METADATA_KEY] = {
        "project_path": str(project),
        "access_mode": "full",
        "future_extension": "x" * 5000,
    }
    manager.save(scoped)
    explicit_null = manager.get_or_create("websocket:null")
    explicit_null.metadata[WORKSPACE_SCOPE_METADATA_KEY] = None
    manager.save(explicit_null)
    manager.save(manager.get_or_create("websocket:missing"))

    rows = {row["key"]: row for row in list_webui_sessions(manager)}

    assert session_list_index.indexed_workspace_scope(rows["websocket:scoped"]) == (
        True,
        {"project_path": str(project), "access_mode": "full"},
    )
    assert session_list_index.indexed_workspace_scope(rows["websocket:null"]) == (True, None)
    assert session_list_index.indexed_workspace_scope(rows["websocket:missing"]) == (False, None)

    scoped.metadata[WORKSPACE_SCOPE_METADATA_KEY]["access_mode"] = "restricted"
    manager.save(scoped)

    refreshed = {row["key"]: row for row in list_webui_sessions(manager)}
    assert session_list_index.indexed_workspace_scope(refreshed["websocket:scoped"])[1] == {
        "project_path": str(project),
        "access_mode": "restricted",
    }


# moeka: upstream's `test_webui_session_list_does_not_cache_old_snapshot_with_new_signature`
# exercised a race between its jsonl-file-scan cache and a concurrent writer
# (racing `open()` mid-read). moeka's SQLite store has no such file-read
# race — every read is one atomic SQL query — so that scenario doesn't
# apply; `indexed_workspace_scope`'s return shape is already covered by
# `test_webui_session_list_indexes_workspace_scope_and_preserves_null` above.


def test_webui_session_list_rejects_invalid_internal_model_preset_metadata(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:custom-metadata")
    session.metadata["model_preset"] = 7
    session.metadata[SESSION_MODEL_PRESET_METADATA_KEY] = {"invalid": True}
    session.add_message("user", "custom metadata")
    manager.save(session)

    with pytest.raises(ValueError, match="session model preset must be a non-empty string"):
        list_webui_sessions(manager)

    assert manager.get_or_create(session.key).metadata["model_preset"] == 7


def test_webui_session_list_rescans_only_changed_file(tmp_path: Path) -> None:
    """Updating one session's messages must not affect any other session's
    listing — the SQLite store has no per-file cache to go stale."""
    manager = _manager(tmp_path)
    first = manager.get_or_create("websocket:first")
    first.add_message("user", "first")
    manager.save(first)
    second = manager.get_or_create("websocket:second")
    second.add_message("user", "second before")
    manager.save(second)

    assert {row["preview"] for row in list_webui_sessions(manager)} == {"first", "second before"}

    second.messages.clear()
    second.add_message("user", "second after")
    manager.save(second)

    rows = list_webui_sessions(manager)

    assert {row["preview"] for row in rows} == {"first", "second after"}


def test_webui_session_list_skips_provider_state_before_preview_budget(
    tmp_path: Path,
) -> None:
    """provider_state lives in the metadata column, never the messages table,
    so it can never be mistaken for preview content regardless of budget."""
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:private-state")
    session.provider_state = ProviderConversationState(
        kind="openai_responses",
        provider="openai:test",
        model="test-model",
        version=1,
        payload={"items": [{"encrypted_content": "x" * 200}]},
    )
    session.add_message("user", "visible preview")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "visible preview"


def test_webui_session_list_drops_deleted_index_rows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:deleted")
    session.add_message("user", "gone")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["key"] == "websocket:deleted"

    assert manager.delete_session("websocket:deleted") is True

    assert list_webui_sessions(manager) == []


def test_webui_session_list_ignores_legacy_stem(tmp_path: Path) -> None:
    """A stray jsonl file sitting in the store's own (out-of-workspace)
    directory is never scanned — only <workspace>/sessions/*.jsonl (the
    legacy pre-SQLite layout) is examined for one-time import."""
    manager = _manager(tmp_path)
    legacy_path = get_store(manager).sessions_dir / "websocket_legacy.jsonl"
    legacy_path.write_text(
        '{"_type":"metadata","key":"websocket:legacy",'
        '"created_at":"2025-01-01T00:00:00",'
        '"updated_at":"2025-01-01T00:00:00","metadata":{}}\n',
        encoding="utf-8",
    )

    assert list_webui_sessions(manager) == []
    assert legacy_path.exists()


def test_webui_session_list_skips_cron_internal_user_preview(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:cron-preview")
    session.add_message(
        "user",
        "Scheduled cron job triggered: 30s-test\n\nInternal reminder prompt",
        **{CRON_HISTORY_META: True},
    )
    session.add_message("assistant", "提醒已经到期。")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "提醒已经到期。"


def test_webui_session_list_skips_trigger_internal_user_preview(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:trigger-preview")
    session.add_message(
        "user",
        "Local trigger received: PR review",
        **{AUTOMATION_HISTORY_META: {"kind": "local_trigger", "trigger_id": "trg_123"}},
    )
    session.add_message("assistant", "PR #4502 已经开始 review。")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "PR #4502 已经开始 review。"


def test_webui_session_list_skips_hidden_history_user_preview(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:hidden-preview")
    session.add_message(
        "user",
        "internal subagent result",
        **{HIDDEN_HISTORY_META: {"kind": "subagent_result", "subagent_task_id": "sub-1"}},
    )
    session.add_message("assistant", "subagent summary")
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "subagent summary"


def test_webui_session_list_uses_webui_transcript_activity_for_sort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    monkeypatch.setattr(session_list_index, "get_webui_dir", lambda: webui_dir)

    manager = _manager(tmp_path)
    old_session = manager.get_or_create("websocket:old-metadata")
    old_session.created_at = datetime(2026, 6, 15, 10, 0, 0)
    old_session.updated_at = datetime(2026, 6, 15, 10, 0, 0)
    old_session.add_message("user", "old metadata")
    old_session.messages[-1]["timestamp"] = "2026-06-15T10:00:00"
    old_session.updated_at = datetime(2026, 6, 15, 10, 0, 0)
    manager.save(old_session)

    newer_metadata = manager.get_or_create("websocket:newer-metadata")
    newer_metadata.created_at = datetime(2026, 6, 15, 11, 0, 0)
    newer_metadata.updated_at = datetime(2026, 6, 15, 11, 0, 0)
    newer_metadata.add_message("user", "newer metadata")
    newer_metadata.messages[-1]["timestamp"] = "2026-06-15T11:00:00"
    newer_metadata.updated_at = datetime(2026, 6, 15, 11, 0, 0)
    manager.save(newer_metadata)

    transcript = webui_dir / "websocket_old-metadata.jsonl"
    transcript.write_text(
        '{"event":"turn_end","chat_id":"old-metadata"}\n',
        encoding="utf-8",
    )
    activity_ns = int(datetime(2026, 6, 15, 12, 0, 0).timestamp() * 1_000_000_000)
    os.utime(transcript, ns=(activity_ns, activity_ns))

    rows = list_webui_sessions(manager)

    assert [row["key"] for row in rows] == [
        "websocket:old-metadata",
        "websocket:newer-metadata",
    ]
    assert rows[0]["updated_at"].startswith("2026-06-15T12:00:00")


def test_webui_session_list_rescans_when_transcript_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    monkeypatch.setattr(session_list_index, "get_webui_dir", lambda: webui_dir)

    manager = _manager(tmp_path)
    session = manager.get_or_create("websocket:transcript-change")
    session.created_at = datetime(2026, 6, 15, 10, 0, 0)
    session.updated_at = datetime(2026, 6, 15, 10, 0, 0)
    session.add_message("user", "preview")
    session.messages[-1]["timestamp"] = "2026-06-15T10:00:00"
    session.updated_at = datetime(2026, 6, 15, 10, 0, 0)
    manager.save(session)

    assert list_webui_sessions(manager)[0]["preview"] == "preview"

    transcript = webui_dir / "websocket_transcript-change.jsonl"
    transcript.write_text(
        '{"event":"turn_end","chat_id":"transcript-change"}\n',
        encoding="utf-8",
    )
    activity_ns = int(datetime(2026, 6, 15, 12, 30, 0).timestamp() * 1_000_000_000)
    os.utime(transcript, ns=(activity_ns, activity_ns))

    rows = list_webui_sessions(manager)

    assert rows[0]["updated_at"].startswith("2026-06-15T12:30:00")


def test_webui_session_list_sorts_by_message_activity_not_maintenance_timestamp(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    old = manager.get_or_create("websocket:old")
    old.created_at = datetime(2026, 6, 1, 10, 0, 0)
    old.add_message("user", "old first visible activity")
    old.messages[-1]["timestamp"] = "2026-06-01T10:00:00"
    old.add_message("assistant", "automation result")
    old.messages[-1]["timestamp"] = "2026-06-05T10:00:00"
    old.updated_at = datetime(2026, 6, 30, 17, 40, 0)
    manager.save(old)

    newer = manager.get_or_create("websocket:newer")
    newer.created_at = datetime(2026, 6, 4, 10, 0, 0)
    newer.add_message("user", "newer real activity")
    newer.messages[-1]["timestamp"] = "2026-06-04T10:00:00"
    newer.updated_at = datetime(2026, 6, 4, 10, 0, 0)
    manager.save(newer)

    rows = list_webui_sessions(manager)

    assert [row["key"] for row in rows] == ["websocket:old", "websocket:newer"]
    assert rows[0]["updated_at"] == "2026-06-05T10:00:00"


def list_webui_sessions(manager: SessionManager) -> list[dict]:
    return session_list_index.list_webui_sessions(manager)


def _write_legacy_jsonl_missing_timestamps(manager: SessionManager, key: str) -> None:
    """Write a legacy per-session jsonl file with no created_at/updated_at,
    for the one-time import path exercised at SessionManager construction.

    Legacy per-session jsonl files live directly under
    <workspace>/sessions/ (moeka's pre-SQLite layout) — the one-time
    import scans that dir, not the (out-of-workspace, ADR-0001)
    SqliteSessionStore.sessions_dir.
    """
    stem = SessionManager.safe_key(key)
    legacy_dir = manager.workspace / "sessions"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    path = legacy_dir / f"{stem}.jsonl"
    path.write_text(
        f'{{"_type": "metadata", "key": "{key}"}}\n'
        '{"role": "user", "content": "hello"}\n',
        encoding="utf-8",
    )


def test_webui_session_list_fallback_time_when_missing(tmp_path: Path) -> None:
    """A legacy jsonl file with no timestamps still gets a usable created_at
    / updated_at after import (Session defaults to "now", never None)."""
    manager = _manager(tmp_path)
    _write_legacy_jsonl_missing_timestamps(manager, "websocket:missing-time")
    manager = _manager(tmp_path)  # re-open to trigger the one-time import

    rows = list_webui_sessions(manager)
    assert len(rows) == 1
    assert rows[0]["key"] == "websocket:missing-time"
    assert rows[0]["created_at"] is not None
    assert rows[0]["updated_at"] is not None
    datetime.fromisoformat(rows[0]["created_at"])
    datetime.fromisoformat(rows[0]["updated_at"])


def test_session_manager_list_sessions_fallback_time_when_missing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _write_legacy_jsonl_missing_timestamps(manager, "websocket:missing-time2")
    manager = _manager(tmp_path)  # re-open to trigger the one-time import

    sessions = manager.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["key"] == "websocket:missing-time2"
    assert sessions[0]["created_at"] is not None
    assert sessions[0]["updated_at"] is not None
    datetime.fromisoformat(sessions[0]["created_at"])
    datetime.fromisoformat(sessions[0]["updated_at"])

