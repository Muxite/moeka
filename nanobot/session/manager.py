"""Session management for conversation history."""

import json
import re
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    image_placeholder_text,
    safe_filename,
)
from nanobot.utils.subagent_channel_display import scrub_subagent_announce_body

FILE_MAX_MESSAGES = 2000
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")
_TOOL_CALL_ECHO_RE = re.compile(r'^\s*(?:generate_image|message)\([^)]*\)\s*$')
_SESSION_PREVIEW_MAX_CHARS = 120


def _sanitize_assistant_replay_text(content: str) -> str:
    """Remove internal replay artifacts that the model may have copied before.

    These strings are useful as runtime/session metadata, but when they appear
    in assistant examples they become demonstrations for the model to repeat.
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line)
        and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


def _text_preview(content: Any) -> str:
    """Return compact display text for session lists."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    text = _sanitize_assistant_replay_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SESSION_PREVIEW_MAX_CHARS:
        text = text[: _SESSION_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def _message_preview_text(message: dict[str, Any]) -> str:
    """Session list preview text; subagent inject blobs are shortened for display."""
    content: Any = message.get("content")
    if message.get("injected_event") == "subagent_result" and isinstance(content, str):
        content = scrub_subagent_announce_body(content)
    return _text_preview(content)


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    @staticmethod
    def _annotate_message_time(message: dict[str, Any], content: Any) -> Any:
        """Expose persisted turn timestamps to the model for relative-date reasoning.

        Annotating *every* assistant turn trains the model (via in-context
        demonstrations) to start its own replies with the same
        ``[Message Time: ...]`` prefix, which leaks metadata back to the user.
        We therefore only annotate user turns. User-side stamps are enough to
        pin adjacent assistant replies for relative-time reasoning, including
        proactive messages the user replies to later.
        """
        timestamp = message.get("timestamp")
        if not timestamp or not isinstance(content, str):
            return content
        role = message.get("role")
        if role != "user":
            return content
        return f"[Message Time: {timestamp}]\n{content}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 120,
        *,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input.

        History is sliced by message count first (``max_messages``), then by
        token budget from the tail (``max_tokens``) when provided.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        max_messages = max_messages if max_messages > 0 else 120
        sliced = unconsolidated[-max_messages:]

        # Avoid starting mid-turn when possible, except for proactive
        # assistant deliveries that the user may be replying to.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if message.get("_command"):
                continue
            content = message.get("content", "")
            role = message.get("role")
            if role == "assistant" and isinstance(content, str):
                content = _sanitize_assistant_replay_text(content)
            # Synthesize an ``[image: path]`` breadcrumb from the persisted
            # ``media`` kwarg so LLM replay still sees *something* where the
            # image used to be. Without this, an image-only user turn
            # replays as an empty user message — the assistant's reply then
            # looks like it's responding to nothing.
            media = message.get("media")
            if role == "user" and isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    image_placeholder_text(p) for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if include_timestamps:
                content = self._annotate_message_time(message, content)
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(key in message for key in ("tool_calls", "reasoning_content", "thinking_blocks")):
                    continue
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # Keep history aligned to the first visible user turn.
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Tight token budgets can otherwise leave assistant-only tails.
                # If a user turn exists in the unsliced output, recover the
                # nearest one even if it slightly exceeds the token budget.
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # And keep a legal tool-call boundary at the front.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix constrained by a hard message cap."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        retained = list(self.messages[-max_messages:])

        # Prefer starting at a user turn when one exists within the tail.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        else:
            # If the tail is assistant/tool-only, anchor to the latest user in
            # the full session and take a capped forward window from there.
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = list(self.messages[latest_user: latest_user + max_messages])

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee: never keep more than max_messages.
        if len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        before = list(self.messages)
        before_last_consolidated = self.last_consolidated
        before_count = len(before)
        self.retain_recent_legal_suffix(limit)
        dropped_count = before_count - len(self.messages)
        if dropped_count <= 0:
            return

        dropped = before[:dropped_count]
        already_consolidated = min(before_last_consolidated, dropped_count)
        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            dropped_count,
            len(archive_chunk),
            len(self.messages),
        )


class SessionManager:
    """Manages conversation sessions.

    Sessions are stored in a single SQLite database (``sessions.db``, WAL
    mode) under the workspace. SQLite's locking replaces the old per-file
    FileLock for cross-process safety; messages are kept as one JSON blob per
    row so heterogeneous message dicts round-trip exactly. Legacy per-session
    ``.jsonl`` files are imported once on startup (then renamed to
    ``*.jsonl.imported`` as a backup).
    """

    _SCHEMA_VERSION = 1

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self.db_path = self.workspace / "sessions.db"
        self._cache: dict[str, Session] = {}
        self._conn_obj: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()
        self._ensure_schema()
        self._import_legacy_jsonl()

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper used by HTTP handlers to map an arbitrary key to a stable filename stem."""
        return safe_filename(key.replace(":", "_"))

    # ------------------------------------------------------------------
    # SQLite plumbing
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._conn_obj is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._conn_obj = conn
        return self._conn_obj

    def _ensure_schema(self) -> None:
        conn = self._conn()
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{{}}',
                last_consolidated INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                session_key TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT,
                created_at TEXT,
                data TEXT NOT NULL,
                PRIMARY KEY (session_key, seq)
            );
            PRAGMA user_version = {self._SCHEMA_VERSION};
        """)
        conn.commit()

    def _import_legacy_jsonl(self) -> None:
        """One-time import of per-session ``.jsonl`` files into sessions.db.

        Imported files are renamed to ``*.jsonl.imported`` (kept as backup,
        never deleted). Only THIS workspace's own sessions directory is
        scanned — never the global legacy dir: a scoped/ephemeral workspace
        must not consume another install's session files into its throwaway
        db. (For the primary workspace the legacy dir *is* its sessions dir,
        so the old-layout migration still happens.)
        """
        candidates: list[Path] = sorted(self.sessions_dir.glob("*.jsonl"))
        if not candidates:
            return
        imported = 0
        for path in candidates:
            try:
                session = self._parse_jsonl(path)
                if session is not None:
                    row = self._conn().execute(
                        "SELECT updated_at FROM sessions WHERE key = ?", (session.key,)
                    ).fetchone()
                    # Newer-wins: a jsonl written after the db row (e.g. by an
                    # old-code process that ran during the migration window)
                    # replaces it; otherwise the db copy is kept.
                    if row is None or row[0] < session.updated_at.isoformat():
                        self.save(session)
                        self._cache.pop(session.key, None)
                        imported += 1
                # Rename unconditionally (even unparseable files) so the same
                # file is never re-parsed on every startup.
                path.rename(path.with_suffix(".jsonl.imported"))
            except Exception:
                logger.exception("Failed to import legacy session file {}", path)
        if imported:
            logger.info(
                "Imported {} legacy jsonl session(s) into {}", imported, self.db_path
            )

    @staticmethod
    def _parse_jsonl(path: Path) -> Session | None:
        """Tolerantly parse a legacy jsonl session file (corrupt lines skipped)."""
        messages: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        key: str | None = None
        created_at: datetime | None = None
        updated_at: datetime | None = None
        last_consolidated = 0
        skipped = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if data.get("_type") == "metadata":
                    metadata = data.get("metadata", {})
                    key = data.get("key") or key
                    if data.get("created_at"):
                        with suppress(ValueError, TypeError):
                            created_at = datetime.fromisoformat(data["created_at"])
                    if data.get("updated_at"):
                        with suppress(ValueError, TypeError):
                            updated_at = datetime.fromisoformat(data["updated_at"])
                    last_consolidated = data.get("last_consolidated", 0)
                else:
                    messages.append(data)
        if skipped:
            logger.warning("Skipped {} corrupt line(s) importing {}", skipped, path)
        if key is None:
            key = path.stem.replace("_", ":", 1)
        if not messages and not metadata:
            return None
        return Session(
            key=key,
            messages=messages,
            created_at=created_at or datetime.now(),
            updated_at=updated_at or datetime.now(),
            metadata=metadata,
            last_consolidated=last_consolidated,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self, key: str) -> Session:
        """Get an existing session or create a new one."""
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from the database."""
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT created_at, updated_at, metadata, last_consolidated"
                " FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            messages: list[dict[str, Any]] = []
            for (data,) in conn.execute(
                "SELECT data FROM messages WHERE session_key = ? ORDER BY seq", (key,)
            ):
                try:
                    messages.append(json.loads(data))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt message row in session {}", key)
            created_at = updated_at = None
            with suppress(ValueError, TypeError):
                created_at = datetime.fromisoformat(row[0])
            with suppress(ValueError, TypeError):
                updated_at = datetime.fromisoformat(row[1])
            try:
                metadata = json.loads(row[2]) if row[2] else {}
            except json.JSONDecodeError:
                metadata = {}
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=row[3] or 0,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Persist a session in one transaction (full replace of its rows).

        SQLite WAL + the transaction give atomicity; concurrent writers from
        other processes are serialized by SQLite's own locking (busy_timeout
        retries). When *fsync* is ``True`` the WAL is checkpointed so the
        write is durable on filesystems with write-back caching.
        """
        conn = self._conn()
        rows = []
        for seq, msg in enumerate(session.messages):
            rows.append((
                session.key,
                seq,
                msg.get("role"),
                msg.get("timestamp"),
                json.dumps(msg, ensure_ascii=False),
            ))
        with self._write_lock:
            with conn:  # one transaction
                conn.execute(
                    "INSERT INTO sessions(key, created_at, updated_at, metadata,"
                    " last_consolidated) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " created_at = excluded.created_at,"
                    " updated_at = excluded.updated_at,"
                    " metadata = excluded.metadata,"
                    " last_consolidated = excluded.last_consolidated",
                    (
                        session.key,
                        session.created_at.isoformat(),
                        session.updated_at.isoformat(),
                        json.dumps(session.metadata, ensure_ascii=False),
                        session.last_consolidated,
                    ),
                )
                conn.execute(
                    "DELETE FROM messages WHERE session_key = ?", (session.key,)
                )
                conn.executemany(
                    "INSERT INTO messages(session_key, seq, role, created_at, data)"
                    " VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            if fsync:
                with suppress(sqlite3.OperationalError):
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        self._cache[session.key] = session

    def flush_all(self) -> int:
        """Re-save every cached session with fsync for durable shutdown.

        Returns the number of sessions flushed.  Errors on individual
        sessions are logged but do not prevent other sessions from being
        flushed.
        """
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """Remove a session from the database and the in-memory cache.

        Returns True if a stored session was found and deleted.
        """
        self.invalidate(key)
        try:
            conn = self._conn()
            with self._write_lock, conn:
                cur = conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
                conn.execute("DELETE FROM messages WHERE session_key = ?", (key,))
            return cur.rowcount > 0
        except Exception as e:
            logger.warning("Failed to delete session {}: {}", key, e)
            return False

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Load a session without caching; intended for read-only HTTP endpoints.

        Returns ``{"key", "created_at", "updated_at", "metadata", "messages"}`` or
        ``None`` when the session does not exist.
        """
        session = self._load(key)
        if session is None:
            return None
        return self._session_payload(session)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions (most recently updated first) with a short preview."""
        sessions: list[dict[str, Any]] = []
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT key, created_at, updated_at, metadata FROM sessions"
                " ORDER BY updated_at DESC"
            ).fetchall()
        except Exception:
            logger.exception("Failed to list sessions")
            return []
        for row in rows:
            try:
                metadata = json.loads(row[3]) if row[3] else {}
            except json.JSONDecodeError:
                metadata = {}
            title = metadata.get("title") if isinstance(metadata, dict) else None
            sessions.append({
                "key": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "title": title if isinstance(title, str) else "",
                "preview": self._preview(row[0]),
                "path": str(self.db_path),
            })
        return sessions

    def _preview(self, key: str) -> str:
        """First user message preview (assistant fallback), like the old file scan."""
        fallback = ""
        try:
            for (data,) in self._conn().execute(
                "SELECT data FROM messages WHERE session_key = ?"
                " ORDER BY seq LIMIT 100",
                (key,),
            ):
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                text = _message_preview_text(item)
                if not text:
                    continue
                if item.get("role") == "user":
                    return text
                if not fallback and item.get("role") == "assistant":
                    fallback = text
        except Exception:
            logger.exception("Failed to build preview for session {}", key)
        return fallback

    def dump_jsonl(self, key: str) -> str | None:
        """Export one session in the legacy jsonl format (for debugging)."""
        session = self._load(key)
        if session is None:
            return None
        lines = [json.dumps({
            "_type": "metadata",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "last_consolidated": session.last_consolidated,
        }, ensure_ascii=False)]
        lines += [json.dumps(msg, ensure_ascii=False) for msg in session.messages]
        return "\n".join(lines) + "\n"
