"""WebUI session list: sidebar rows layered on the core SQLite SessionManager.

The core ``SessionManager`` already lists sessions efficiently via a single
SQLite query (no re-scanning needed, unlike the old per-file jsonl store this
module was originally written against). This module adds the WebUI-only
concerns on top: reconciling each session's "visible activity" timestamp
against out-of-band WebUI activity files (so purely-internal housekeeping
writes don't bump a session to the top of the sidebar), exposing the
per-session model preset, and recovering sessions that exist only as a WebUI
transcript (no canonical ``sessions.db`` row yet, e.g. a crash between the
first WebUI turn and the agent's own session save).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from nanobot.config.paths import get_webui_dir
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.session.manager import SessionManager, _message_preview_text
from nanobot.session.sqlite_store import (
    _WORKSPACE_SCOPE_PRESENT_FIELD,
    _WORKSPACE_SCOPE_VALUE_FIELD,
    _indexed_workspace_scope_fields,
)

_MODEL_PRESET_FIELD = "model_preset"
_VISIBLE_TRANSCRIPT_ROLES = {"user", "assistant"}
_SESSION_LIST_PREVIEW_MAX_RECORDS = 200
_SESSION_LIST_PREVIEW_MAX_CHARS = 1_000_000
_WEBUI_SESSION_STEM_PREFIX = SessionManager.safe_key("websocket:")
_WEBUI_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")
_TRANSCRIPT_SEGMENTS_SUFFIX = ".segments"
_TRANSCRIPT_NON_ANSWER_KINDS = {"progress", "reasoning", "tool_hint"}

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

    known_keys = {row.get("key") for row in rows}
    webui_dir = get_webui_dir()
    for stem, paths in _webui_transcript_sources(webui_dir).items():
        if any(
            isinstance(key, str) and SessionManager.safe_key(key) == stem
            for key in known_keys
        ):
            continue
        recovered = _scan_transcript_row(None, stem, paths, webui_dir)
        if recovered is not None and recovered["key"] not in known_keys:
            sessions.append(recovered)
            known_keys.add(recovered["key"])

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
    return _webui_activity_paths_in(stem, webui_dir)


def _webui_activity_paths_in(stem: str, webui_dir: Path) -> list[Path]:
    paths = [
        webui_dir / f"{stem}.jsonl",
        webui_dir / f"{stem}.json",
    ]
    segments_dir = webui_dir / f"{stem}{_TRANSCRIPT_SEGMENTS_SUFFIX}"
    if segments_dir.is_dir() and not segments_dir.is_symlink():
        try:
            paths.extend(
                sorted(
                    path
                    for path in segments_dir.iterdir()
                    if path.is_file() and not path.is_symlink()
                )
            )
        except OSError:
            pass
    return paths


def _webui_activity_signature(session_key: str) -> dict[str, int]:
    stem = SessionManager.safe_key(session_key)
    latest_mtime_ns = 0
    total_size = 0
    for path in _webui_activity_paths_in(stem, get_webui_dir()):
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


# ---------------------------------------------------------------------------
# Transcript-only session recovery (upstream feature, ported onto the SQLite
# backend): a WebUI transcript can exist with no matching ``sessions.db`` row
# (e.g. a crash between the first WebUI turn and the agent's own session
# save). Without this, such a session would be invisible in the sidebar even
# though its transcript is still on disk and reloadable.
# ---------------------------------------------------------------------------


def _webui_transcript_record_paths(stem: str, webui_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    segments_dir = webui_dir / f"{stem}{_TRANSCRIPT_SEGMENTS_SUFFIX}"
    if segments_dir.is_dir() and not segments_dir.is_symlink():
        try:
            paths.extend(
                sorted(
                    path
                    for path in segments_dir.glob("*.jsonl")
                    if path.is_file() and not path.is_symlink()
                )
            )
        except OSError:
            pass
    active = webui_dir / f"{stem}.jsonl"
    if active.is_file() and not active.is_symlink():
        paths.append(active)
    return tuple(paths)


def _webui_transcript_sources(webui_dir: Path) -> dict[str, tuple[Path, ...]]:
    stems: set[str] = set()
    try:
        entries = tuple(webui_dir.iterdir())
    except OSError:
        return {}
    for path in entries:
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix == ".jsonl":
            stem = path.stem
        elif path.is_dir() and path.name.endswith(_TRANSCRIPT_SEGMENTS_SUFFIX):
            stem = path.name.removesuffix(_TRANSCRIPT_SEGMENTS_SUFFIX)
        else:
            continue
        if stem.startswith(_WEBUI_SESSION_STEM_PREFIX):
            stems.add(stem)
    return {
        stem: paths
        for stem in sorted(stems)
        if (paths := _webui_transcript_record_paths(stem, webui_dir))
    }


def _transcript_record(line: str) -> dict[str, Any] | None:
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _valid_transcript_session_key(key: str, stem: str) -> bool:
    if not key.startswith("websocket:"):
        return False
    chat_id = key.split(":", 1)[1]
    return _WEBUI_CHAT_ID_RE.fullmatch(chat_id) is not None and SessionManager.safe_key(key) == stem


def _transcript_preview(record: dict[str, Any]) -> tuple[str, str]:
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return "", ""
    preview = _message_preview_text({"content": text})
    if not preview:
        return "", ""
    event = record.get("event")
    if event == "user" or record.get("role") == "user":
        return preview, ""
    if (
        event == "message"
        and record.get("kind") not in _TRANSCRIPT_NON_ANSWER_KINDS
    ) or record.get("role") == "assistant":
        return "", preview
    return "", ""


def _transcript_created_at(record: dict[str, Any]) -> str | None:
    value = record.get("created_at_ms")
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or value < 0
    ):
        return None
    try:
        return datetime.fromtimestamp(value / 1000).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _scan_transcript_row(
    session_key: str | None,
    stem: str,
    paths: tuple[Path, ...],
    webui_dir: Path,
) -> dict[str, Any] | None:
    path_key = session_key or f"websocket:{stem.removeprefix(_WEBUI_SESSION_STEM_PREFIX)}"
    signature = _webui_activity_signature(path_key)
    activity_updated_at = _webui_activity_updated_at(signature)
    if activity_updated_at is None:
        return None

    preview = ""
    fallback_preview = ""
    created_at: str | None = None
    saw_record = False
    scanned_records = 0
    scanned_chars = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    scanned_records += 1
                    scanned_chars += len(line)
                    record = _transcript_record(line)
                    if record is not None:
                        saw_record = True
                        chat_id = record.get("chat_id")
                        if isinstance(chat_id, str) and chat_id.strip():
                            candidate = f"websocket:{chat_id.strip()}"
                            if _valid_transcript_session_key(candidate, stem):
                                session_key = candidate
                        if created_at is None:
                            created_at = _transcript_created_at(record)
                        user_preview, assistant_preview = _transcript_preview(record)
                        if user_preview:
                            preview = user_preview
                            break
                        if not fallback_preview and assistant_preview:
                            fallback_preview = assistant_preview
                    if (
                        scanned_records >= _SESSION_LIST_PREVIEW_MAX_RECORDS
                        or scanned_chars >= _SESSION_LIST_PREVIEW_MAX_CHARS
                    ):
                        break
        except OSError:
            continue
        if preview or (
            scanned_records >= _SESSION_LIST_PREVIEW_MAX_RECORDS
            or scanned_chars >= _SESSION_LIST_PREVIEW_MAX_CHARS
        ):
            break
    if not saw_record:
        return None
    if session_key is None:
        fallback = f"websocket:{stem.removeprefix(_WEBUI_SESSION_STEM_PREFIX)}"
        if not _valid_transcript_session_key(fallback, stem):
            return None
        session_key = fallback

    if created_at is None:
        try:
            earliest_mtime = min(path.stat().st_mtime for path in paths)
            created_at = datetime.fromtimestamp(earliest_mtime).isoformat()
        except (OSError, OverflowError, ValueError):
            created_at = activity_updated_at
    return {
        "key": session_key,
        "created_at": created_at,
        "updated_at": activity_updated_at,
        "title": "",
        "preview": preview or fallback_preview,
        _MODEL_PRESET_FIELD: None,
        "path": str(webui_dir / f"{stem}.jsonl"),
        **_indexed_workspace_scope_fields({}),
    }
