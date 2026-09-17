"""WebUI session list: sidebar rows layered on the core SQLite SessionManager.

The core ``SessionManager`` already lists sessions efficiently via a single
SQLite query (no re-scanning needed, unlike the old per-file jsonl store this
module was originally written against). This module adds the WebUI-only
concerns on top: reconciling each session's "visible activity" timestamp
against out-of-band WebUI activity files (so purely-internal housekeeping
writes don't bump a session to the top of the sidebar) and exposing the
per-session model preset.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from nanobot.config.paths import get_webui_dir
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.session.manager import SessionManager
from nanobot.session.sqlite_store import (
    _WORKSPACE_SCOPE_PRESENT_FIELD,
    _WORKSPACE_SCOPE_VALUE_FIELD,
)

_MODEL_PRESET_FIELD = "model_preset"
_VISIBLE_TRANSCRIPT_ROLES = {"user", "assistant"}

# moeka: upstream (2026-08-05 checkpoint) added a persistent
# ``.webui_session_index.json`` cache here to avoid re-scanning per-session
# jsonl files on every sidebar load. moeka's SqliteSessionStore already
# answers that query in one SQLite statement (see module docstring), so the
# cache file itself is dropped — but these field names are still a real
# public contract consumed by nanobot/webui/ws_http.py, and the *values* are
# computed once per row in SqliteSessionStore.list_sessions() (from the same
# metadata that query already fetched), not re-read per session here.
WEBUI_SESSION_INDEX_INTERNAL_FIELDS = frozenset(
    {_WORKSPACE_SCOPE_PRESENT_FIELD, _WORKSPACE_SCOPE_VALUE_FIELD}
)


def indexed_workspace_scope(row: dict[str, Any]) -> tuple[bool, object]:
    """Return the workspace-scope value carried on a session row."""
    return (
        row.get(_WORKSPACE_SCOPE_PRESENT_FIELD) is True,
        cast(object, row.get(_WORKSPACE_SCOPE_VALUE_FIELD)),
    )


def list_webui_sessions(session_manager: SessionManager) -> list[dict[str, Any]]:
    """Return session rows for the WebUI sidebar, most recently active first."""
    rows = session_manager.list_sessions()
    sessions = [_public_row(session_manager, row) for row in rows]
    return sorted(sessions, key=lambda row: row.get("updated_at", ""), reverse=True)


def _public_row(session_manager: SessionManager, row: dict[str, Any]) -> dict[str, Any]:
    key = row.get("key")
    activity_signature = _webui_activity_signature(str(key)) if key else _EMPTY_ACTIVITY
    activity_updated_at = _webui_activity_updated_at(activity_signature)
    visible_message_at = _last_visible_message_at(session_manager, key) if key else None
    public_row: dict[str, Any] = {
        "key": key,
        "created_at": row.get("created_at"),
        "updated_at": _visible_activity_updated_at(
            row.get("updated_at"),
            visible_message_at,
            activity_updated_at,
        ),
        "title": row.get("title", ""),
        "preview": row.get("preview", ""),
        _MODEL_PRESET_FIELD: row.get(_MODEL_PRESET_FIELD),
        "path": row.get("path"),
        _WORKSPACE_SCOPE_PRESENT_FIELD: row.get(_WORKSPACE_SCOPE_PRESENT_FIELD),
        _WORKSPACE_SCOPE_VALUE_FIELD: row.get(_WORKSPACE_SCOPE_VALUE_FIELD),
    }
    return public_row


_EMPTY_ACTIVITY: dict[str, int] = {"webui_activity_mtime_ns": 0, "webui_activity_size": 0}


def _webui_activity_paths(session_key: str) -> list[Path]:
    stem = SessionManager.safe_key(session_key)
    webui_dir = get_webui_dir()
    return [
        webui_dir / f"{stem}.jsonl",
        webui_dir / f"{stem}.json",
    ]


def _webui_activity_signature(session_key: str) -> dict[str, int]:
    latest_mtime_ns = 0
    total_size = 0
    for path in _webui_activity_paths(session_key):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        total_size += stat.st_size
    return {
        "webui_activity_mtime_ns": latest_mtime_ns,
        "webui_activity_size": total_size,
    }


def _webui_activity_updated_at(signature: dict[str, int]) -> str | None:
    mtime_ns = signature.get("webui_activity_mtime_ns", 0)
    if mtime_ns <= 0:
        return None
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000).isoformat()


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _latest_updated_at(stored: str | None, activity: str | None) -> str | None:
    if _timestamp(activity) > _timestamp(stored):
        return activity
    return stored


def _visible_message_timestamp(item: dict[str, Any]) -> str | None:
    if is_hidden_history_message(item):
        return None
    if item.get("role") not in _VISIBLE_TRANSCRIPT_ROLES:
        return None
    timestamp = item.get("timestamp")
    return timestamp if isinstance(timestamp, str) else None


def _last_visible_message_at(session_manager: SessionManager, key: str) -> str | None:
    """Scan persisted messages for the most recent user/assistant timestamp.

    Bounded to a reasonable tail so a very long session doesn't make sidebar
    listing expensive; the most recent visible activity is what matters for
    sort order.
    """
    try:
        from nanobot.session.sqlite_store import get_store

        conn = get_store(session_manager)._conn()  # noqa: SLF001
        rows = conn.execute(
            "SELECT data FROM messages WHERE session_key = ? ORDER BY seq DESC LIMIT 200",
            (key,),
        ).fetchall()
    except Exception:
        return None
    latest: str | None = None
    for (data,) in rows:
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        timestamp = _visible_message_timestamp(item)
        if timestamp is not None:
            latest = _latest_updated_at(latest, timestamp)
    return latest


def _visible_activity_updated_at(
    stored: str | None,
    visible_message_at: str | None,
    webui_activity: str | None,
) -> str | None:
    return _latest_updated_at(visible_message_at, webui_activity) or stored
