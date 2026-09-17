"""SQLite-backed ``SessionStore`` — moeka's memory backend.

Implements upstream's ``SessionStore`` Protocol (``nanobot/session/manager.py``)
so the ``SessionManager`` wrapper there — caching, ``SessionPolicy``,
``get_or_create_transient``, ``fork_session_before_user_index``, and the
retention/continuity fixes on ``Session`` — stays upstream's own,
unforked file. Only the six backend-specific methods live here:
``load``/``save``/``delete``/``read``/``read_metadata``/``list_sessions``.
Sessions are stored in a single SQLite database (``sessions.db``, WAL
mode); messages are kept as one JSON blob per row so heterogeneous
message dicts round-trip exactly.

ADR-0001 (adopted from upstream): sessions live outside the agent's own
workspace by default. A stable per-workspace identity marker under
``<workspace>/.nanobot/workspace-id`` maps to
``<sessions_root>/<workspace-id>/sessions.db``, so a shell/file tool
running inside the workspace can never read or tamper with session
history. This matters more for moeka than for stock nanobot: moeka's
shell sandbox is deliberately permissive (``nanobot/agent/tools/shell.py``
does not block ``rm -rf``, ``dd``, etc. by default). Location is still
configurable via ``sessions_root``, so a live install's cutover can be
staged explicitly — see ``.agent/upstream-sync-notes.md`` for the exact
procedure.

This reuses ``JsonlSessionStore``'s workspace-identity/namespace-claiming
classmethods directly (``_load_or_create_workspace_id``,
``_claim_workspace_namespace``) rather than duplicating them: that logic
(marker files, atomic writes, symlink guards, copy-vs-move detection) is
100% storage-format-agnostic. This is an intentional coupling to
upstream's "private" (leading-underscore) classmethods; if a future
upstream merge renames or removes them, this file needs a matching
update — see the same notes file for what to check.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from filelock import FileLock
from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir, get_state_home
from nanobot.providers.base import ProviderConversationState
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.session.manager import (
    _SESSION_LIST_PREVIEW_MAX_CHARS,
    _SESSION_LIST_PREVIEW_MAX_RECORDS,
    _SESSION_MIGRATION_LOCK_TIMEOUT_SECONDS,
    JsonlSessionStore,
    Session,
    SessionInfo,
    SessionMetadataPayload,
    SessionPayload,
    SessionRestoreResult,
    _message_preview_text,
    _metadata_title,
)
from nanobot.session.model_selection import model_preset_from_metadata
from nanobot.utils.helpers import ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

# sqlite3.Error is included so SQLite I/O failures (locked db, corrupt file,
# etc.) are handled the same defensive way as malformed data.
_SESSION_DATA_ERRORS = (ValueError, TypeError, AttributeError, KeyError, sqlite3.Error)

# Extra SessionInfo fields for nanobot/webui/session_list_index.py, computed
# here (from the same metadata row list_sessions() already fetched) rather
# than via a second per-session read. Upstream's own JsonlSessionStore
# solves the same "don't re-scan for the WebUI sidebar" problem with a
# persistent .webui_session_index.json cache file (dropped in favor of
# moeka's single SQL query — see nanobot/webui/session_list_index.py's
# module docstring); this mirrors its two computed fields so
# nanobot/webui/session_list_index.py's _public_row() never needs a second
# read_session_metadata() call per row (upstream's
# perf(webui): accelerate JSONL session list and thread loading, #5194,
# has a regression test for exactly that: a session-list handler that
# fails if read_session_metadata is called again).
_WORKSPACE_SCOPE_PRESENT_FIELD = "_workspace_scope_present"
_WORKSPACE_SCOPE_VALUE_FIELD = "_workspace_scope_value"
_INDEXED_WORKSPACE_SCOPE_KEYS = ("project_path", "path", "access_mode")
_MAX_INDEXED_WORKSPACE_SCOPE_BYTES = 4096


def _indexed_workspace_scope_fields(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        return {_WORKSPACE_SCOPE_PRESENT_FIELD: False, _WORKSPACE_SCOPE_VALUE_FIELD: None}
    metadata_data = cast(dict[str, Any], metadata)
    if WORKSPACE_SCOPE_METADATA_KEY not in metadata_data:
        return {_WORKSPACE_SCOPE_PRESENT_FIELD: False, _WORKSPACE_SCOPE_VALUE_FIELD: None}

    raw_scope = metadata_data.get(WORKSPACE_SCOPE_METADATA_KEY)
    indexed_scope: object = False
    if raw_scope is None:
        indexed_scope = None
    elif isinstance(raw_scope, dict):
        scope_data = cast(dict[object, object], raw_scope)
        recognized = {
            key: scope_data[key] for key in _INDEXED_WORKSPACE_SCOPE_KEYS if key in scope_data
        }
        try:
            encoded = json.dumps(recognized, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
        else:
            if len(encoded.encode("utf-8")) <= _MAX_INDEXED_WORKSPACE_SCOPE_BYTES:
                indexed_scope = cast(object, json.loads(encoded))
    return {_WORKSPACE_SCOPE_PRESENT_FIELD: True, _WORKSPACE_SCOPE_VALUE_FIELD: indexed_scope}


def default_sessions_root(canonical_workspace: Path) -> Path:
    """Default ``sessions_root`` when a caller doesn't pass one explicitly.

    Upstream's own default is ``get_runtime_subdir("sessions")`` — a
    subdirectory of ``get_data_dir()`` (``config.json``'s parent). That
    works for upstream's nested layout (workspace is a subdirectory of the
    instance data dir), but moeka's ``AgentDefaults.workspace`` defaults to
    the instance data dir *itself* (``~/.nanobot``, no nested
    ``~/.nanobot/workspace`` — see CLAUDE.md's "flat workspace layout"
    deviation), so ``get_data_dir()/sessions`` would be *inside* the
    workspace for the default install — exactly what ADR-0001 exists to
    prevent, and it would fail the safety check below on every startup.

    A workspace-relative sibling avoids the collision generically, for any
    workspace path (default flat layout, a custom ``MOEKA_WORKSPACE``, or
    an embedding host's arbitrary scoped/ephemeral workspace), without
    depending on ``config.json``'s location at all: ``<workspace>-sessions``
    next to ``<workspace>``.
    """
    return canonical_workspace.parent / f"{canonical_workspace.name}-sessions"


class SqliteSessionStore:
    """Manages session persistence in a single SQLite database.

    Sessions are stored in a single SQLite database (``sessions.db``, WAL
    mode). SQLite's locking replaces a per-file FileLock for cross-process
    safety; messages are kept as one JSON blob per row so heterogeneous
    message dicts round-trip exactly. Legacy per-session ``.jsonl`` files,
    and a legacy in-workspace ``sessions.db`` from before ADR-0001, are
    imported once on startup.
    """

    _SCHEMA_VERSION = 1

    def __init__(self, workspace: Path, *, sessions_root: Path | None = None):
        canonical_workspace = Path(workspace).expanduser().resolve(strict=False)
        ensure_dir(canonical_workspace)
        root = (
            Path(sessions_root).expanduser().resolve(strict=False)
            if sessions_root is not None
            else default_sessions_root(canonical_workspace)
        )
        if root == canonical_workspace or root.is_relative_to(canonical_workspace):
            raise RuntimeError(
                "session storage must be outside the agent workspace; "
                "move --config outside --workspace or choose a nested workspace directory"
            )
        ensure_dir(root)
        with suppress(OSError):
            os.chmod(root, 0o700)
        self.workspace = canonical_workspace

        migration_lock = FileLock(
            str(root / ".workspace-migration.lock"),
            timeout=_SESSION_MIGRATION_LOCK_TIMEOUT_SECONDS,
        )
        with migration_lock:
            workspace_id = JsonlSessionStore._load_or_create_workspace_id(  # noqa: SLF001
                canonical_workspace, root,
            )
            workspace_id = JsonlSessionStore._claim_workspace_namespace(  # noqa: SLF001
                root, canonical_workspace, workspace_id,
            )
            self.sessions_dir = ensure_dir(root / workspace_id)
            self.legacy_sessions_dir = get_legacy_sessions_dir()
            self.db_path = self.sessions_dir / "sessions.db"
            self._conn_obj: sqlite3.Connection | None = None
            self._write_lock = threading.Lock()
            self._warn_if_legacy_db_in_workspace()
            self._ensure_schema()

        # Legacy jsonl import can happen outside the identity-claim lock
        # (it only touches self.sessions_dir, already ours at this point).
        self._import_legacy_jsonl()

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

    def close(self) -> None:
        """Checkpoint the WAL and release the connection.

        Without this the only checkpoint was ``save(fsync=True)``, reached
        solely via ``flush_all()`` at graceful shutdown — so a SIGKILL, an
        OOM, or a TimeoutStopSec expiry left the WAL untruncated, and an
        embedding host creating many short-lived managers leaked one
        SQLite connection each. Idempotent, and safe to call twice.
        """
        conn, self._conn_obj = self._conn_obj, None
        if conn is None:
            return
        try:
            with suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            with suppress(sqlite3.Error):
                conn.close()

    def __enter__(self) -> SqliteSessionStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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

    # ------------------------------------------------------------------
    # One-time migrations
    # ------------------------------------------------------------------

    def _warn_if_legacy_db_in_workspace(self) -> None:
        """Detect a pre-ADR-0001 in-workspace ``sessions.db`` and warn loudly.

        Before ADR-0001, moeka kept ``sessions.db`` directly under the
        workspace (``<workspace>/sessions.db``). This used to be migrated
        automatically (moved out of the workspace) the first time a process
        constructed this store against the new, empty destination. That
        auto-migration is exactly what turned a routine test run into a
        2026-09-17 incident: a code path that resolved a workspace to the
        *real* ~/.nanobot triggered a real, unattended move of the live
        moeka.service database. See .agent/upstream-sync-notes.md's
        incident writeup.

        Startup detects and warns only, now -- it never touches the legacy
        file. Migration is an explicit, opt-in operation:
        ``nanobot sessions migrate --from <legacy> --to <new> [--dry-run]
        [--yes]`` (see ``nanobot/cli/commands.py``). This mirrors the manual
        procedure also documented in .agent/upstream-sync-notes.md, for
        hosts that don't stop the service for a controlled cutover.
        """
        legacy_db = self.workspace / "sessions.db"
        if not legacy_db.exists():
            return
        logger.warning(
            "Legacy in-workspace session database found at {} (this workspace "
            "now stores sessions at {}, per ADR-0001). This will NOT be moved "
            "automatically. Review it and, if it holds data you want kept, run: "
            "nanobot sessions migrate --from {} --to {} --dry-run",
            legacy_db, self.db_path, legacy_db, self.db_path,
        )

    def _import_legacy_jsonl(self) -> None:
        """One-time import of per-session ``.jsonl`` files into sessions.db.

        Imported files are renamed to ``*.jsonl.imported`` (kept as backup,
        never deleted). Only this workspace's own legacy sessions directory
        (``<workspace>/sessions``, the pre-SQLite-migration layout) is
        scanned — never the global legacy dir: a scoped/ephemeral workspace
        must not consume another install's session files into its
        throwaway db.

        Ordering matters: ``SessionManager.__init__`` (upstream, frozen —
        see ``nanobot/session/manager.py``'s module-level note) *always*
        builds its own internal ``JsonlSessionStore``, whose own migration
        (``JsonlSessionStore._migrate_from_workspace``) globs the exact same
        ``<workspace>/sessions/*.jsonl`` files and deletes each source after
        copying it elsewhere. If that ran first, this method would find
        nothing — not a double import (impossible), a silent *drop*. This is
        safe only because this store is always fully constructed (import
        included) *before* ``SessionManager`` exists. That ordering is not
        implicit or accidental: ``build_default_session_manager`` below is
        the one call path production code uses, and it constructs the store
        and calls ``SessionManager`` as two separate, explicit statements in
        that order, specifically so the dependency doesn't rely on a reader
        noticing that a keyword argument gets evaluated before the function
        body it's passed into. See
        ``tests/agent/test_session_atomic.py::TestLegacyJsonlImport::test_jsonl_survives_via_build_default_session_manager``
        for the regression test.
        """
        legacy_dir = self.workspace / "sessions"
        if not legacy_dir.is_dir():
            return
        candidates: list[Path] = sorted(legacy_dir.glob("*.jsonl"))
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
    # SessionStore Protocol
    # ------------------------------------------------------------------

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper used by HTTP handlers to map an arbitrary key to a stable filename stem."""
        return safe_filename(key.replace(":", "_"))

    def load(self, key: str) -> Session | None:
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
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt message row in session {}", key)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Skipping non-object message row in session {}", key)
                    continue
                messages.append(parsed)
            created_at = updated_at = None
            with suppress(ValueError, TypeError):
                created_at = datetime.fromisoformat(row[0])
            with suppress(ValueError, TypeError):
                updated_at = datetime.fromisoformat(row[1])
            try:
                metadata = json.loads(row[2]) if row[2] else {}
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            provider_state = ProviderConversationState.from_private_record(
                metadata.pop("_provider_state", None)
            )
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=row[3] or 0,
                provider_state=provider_state,
            )
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

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
        # provider_state rides along in the metadata blob under a reserved key
        # rather than a dedicated column, so it survives the same atomic
        # replace as the rest of the row without a schema migration.
        metadata_to_store = dict(session.metadata)
        if session.provider_state is not None:
            metadata_to_store["_provider_state"] = session.provider_state.to_private_record()
        else:
            metadata_to_store.pop("_provider_state", None)
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
                        json.dumps(metadata_to_store, ensure_ascii=False),
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

    def delete(self, key: str) -> bool:
        """Delete a session from the database. Returns True if one was found."""
        try:
            conn = self._conn()
            with self._write_lock, conn:
                cur = conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
                conn.execute("DELETE FROM messages WHERE session_key = ?", (key,))
            return cur.rowcount > 0
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to delete session {}: {}", key, e)
            return False

    @staticmethod
    def _session_payload(session: Session) -> SessionPayload:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def read(self, key: str) -> SessionPayload | None:
        """Load a session without caching; intended for read-only HTTP endpoints."""
        session = self.load(key)
        if session is None:
            return None
        return self._session_payload(session)

    def read_metadata(self, key: str) -> SessionMetadataPayload | None:
        """Load only the metadata record for a session, without its messages."""
        try:
            row = self._conn().execute(
                "SELECT key, created_at, updated_at, metadata FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            try:
                metadata = json.loads(row[3]) if row[3] else {}
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            # Never surface the provider-private continuation blob (may hold
            # encrypted reasoning items) through the WebUI metadata route.
            metadata.pop("_provider_state", None)
            return {
                "key": row[0] or key,
                "created_at": row[1],
                "updated_at": row[2],
                "metadata": metadata,
            }
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to read session metadata {}: {}", key, e)
            return None

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions (most recently updated first) with a short preview."""
        sessions: list[SessionInfo] = []
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT key, created_at, updated_at, metadata FROM sessions"
                " ORDER BY updated_at DESC"
            ).fetchall()
        except _SESSION_DATA_ERRORS:
            logger.exception("Failed to list sessions")
            return []
        for row in rows:
            try:
                metadata = json.loads(row[3]) if row[3] else {}
            except json.JSONDecodeError:
                metadata = {}
            info = cast(dict[str, Any], {
                "key": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "title": _metadata_title(metadata),
                "preview": self._preview(row[0]),
                "path": str(self.db_path),
            })
            # Extra keys beyond the Protocol's SessionInfo TypedDict, kept for
            # nanobot/webui/session_list_index.py; extra dict keys are fine
            # for downstream .get() consumers, just not statically declared.
            info["model_preset"] = model_preset_from_metadata(metadata)
            info.update(_indexed_workspace_scope_fields(metadata))
            sessions.append(cast(SessionInfo, info))
        return sessions

    def _preview(self, key: str) -> str:
        """First user message preview (assistant fallback).

        The rows are materialised with ``fetchall()`` *before* scanning
        them: iterating the cursor directly and returning from inside the
        loop leaves an un-exhausted statement on the long-lived shared
        connection, and SQLite keeps a read transaction open for as long
        as that statement is live — blocking WAL checkpointing and
        growing the WAL without bound. The LIMIT keeps the materialised
        set small.
        """
        fallback = ""
        scanned_chars = 0
        try:
            rows = self._conn().execute(
                "SELECT data FROM messages WHERE session_key = ?"
                f" ORDER BY seq LIMIT {_SESSION_LIST_PREVIEW_MAX_RECORDS}",
                (key,),
            ).fetchall()
        except _SESSION_DATA_ERRORS:
            logger.exception("Failed to build preview for session {}", key)
            return fallback

        for (data,) in rows:
            scanned_chars += len(data)
            if scanned_chars > _SESSION_LIST_PREVIEW_MAX_CHARS:
                break
            try:
                item = json.loads(data)
            except json.JSONDecodeError:
                continue
            if is_hidden_history_message(item):
                continue
            text = _message_preview_text(item)
            if not text:
                continue
            if item.get("role") == "user":
                return text
            if not fallback and item.get("role") == "assistant":
                fallback = text
        return fallback

    # ------------------------------------------------------------------
    # moeka-only extras (not part of the SessionStore Protocol; reach these
    # via nanobot.session.sqlite_store.get_store(session_manager))
    # ------------------------------------------------------------------

    def dump_jsonl(self, key: str) -> str | None:
        """Export one session in the legacy jsonl format (for debugging)."""
        session = self.load(key)
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

    def export_to_workspace(self) -> SessionRestoreResult:
        """Export every session as legacy jsonl into ``<workspace>/sessions/``.

        Unlike upstream's ``JsonlSessionStore.restore_to_workspace`` (which
        moves its own out-of-workspace jsonl files back in), moeka's active
        data lives in ``sessions.db`` — this is a plain jsonl export,
        useful before downgrading to a nanobot version that predates the
        SQLite migration. Never overwrites a file whose content already
        matches; anything else on disk at that path is reported as a
        conflict rather than clobbered.
        """
        restored = 0
        unchanged = 0
        conflicts: list[Path] = []
        for row in self.list_sessions():
            key = row.get("key")
            if not isinstance(key, str):
                continue
            dumped = self.dump_jsonl(key)
            if dumped is None:
                continue
            target = self.workspace / "sessions" / f"{self.safe_key(key)}.jsonl"
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8")
                except OSError:
                    existing = None
                if existing == dumped:
                    unchanged += 1
                    continue
                conflicts.append(target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dumped, encoding="utf-8")
            restored += 1
        return SessionRestoreResult(
            restored=restored,
            unchanged=unchanged,
            conflicts=tuple(conflicts),
        )


@dataclass
class SessionDbMigrationPlan:
    """What ``migrate_session_database`` would do, computed without acting.

    ``source_in_use`` is best-effort: it only detects a *currently open,
    lockable* SQLite writer, not "some other process might open it later".
    Callers still need the destination-side integrity check as the real
    safety net.
    """

    source: Path
    destination: Path
    files: tuple[tuple[str, int], ...]  # (suffix label, size in bytes)
    source_in_use: bool
    destination_exists: bool


class SessionDbMigrationError(RuntimeError):
    """Raised when a migration is refused or fails verification."""


def _sqlite_triplet(db_path: Path) -> list[tuple[str, Path]]:
    """Return the (label, path) pairs for a sessions.db + WAL/SHM triplet that exist."""
    pairs = [
        ("db", db_path),
        ("wal", db_path.with_name(db_path.name + "-wal")),
        ("shm", db_path.with_name(db_path.name + "-shm")),
    ]
    return [(label, path) for label, path in pairs if path.exists()]


def _is_sqlite_db_in_use(db_path: Path) -> bool:
    """Best-effort check: can we grab an exclusive write lock right now?

    ``BEGIN IMMEDIATE`` with a zero busy_timeout fails immediately (rather
    than blocking) if another connection already holds the write lock —
    which is exactly the condition that made the 2026-09-17 incident live
    rather than theoretical: a running ``moeka.service`` process had this
    exact file open. Not perfect, and the gap is not academic:

    - A reader-only connection wouldn't block this at all.
    - **An idle connection that simply has the file open — with no write
      transaction in flight — does not hold SQLite's write lock either, so
      this check reports "not in use" even though a process is sitting
      right there.** This is exactly ``moeka.service``'s normal state
      between turns: connected, WAL mode, no active write. Migrating in
      that window can still corrupt the database (e.g. a turn starting
      mid-migration) even though this function said it was safe.

    A positive result here (locked right now) is a hard, reliable "no" —
    that's the side worth failing safe on. A negative result is **not**
    proof the database is safe to move; it only means nothing was actively
    writing at the exact instant this check ran. The only reliable
    precondition is stopping the process first — see
    ``.agent/deploy-runbook.md``, which this check cannot substitute for.
    """
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path), timeout=0)
        try:
            conn.execute("PRAGMA busy_timeout=0")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return False
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True


def plan_session_database_migration(source: Path, destination: Path) -> SessionDbMigrationPlan:
    """Compute (without touching anything) what a migration would move.

    *source* and *destination* are ``sessions.db`` file paths, not
    directories — callers resolve the specific database they mean (e.g.
    ``<workspace>/sessions.db`` for a legacy in-workspace database, or
    ``<sessions_root>/<workspace-id>/sessions.db`` for the ADR-0001
    location) rather than this function guessing.
    """
    source = Path(source).expanduser().resolve(strict=False)
    destination = Path(destination).expanduser().resolve(strict=False)
    triplet = _sqlite_triplet(source)
    files = tuple((label, path.stat().st_size) for label, path in triplet)
    return SessionDbMigrationPlan(
        source=source,
        destination=destination,
        files=files,
        source_in_use=_is_sqlite_db_in_use(source),
        destination_exists=destination.exists(),
    )


def migrate_session_database(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
) -> SessionDbMigrationPlan:
    """Copy a ``sessions.db`` (+ ``-wal``/``-shm``) triplet, verify, then
    delete the source.

    This is the only code path in moeka that moves a session database out
    from under a workspace -- startup (``SqliteSessionStore.__init__``)
    never does this on its own; it only warns (see
    ``_warn_if_legacy_db_in_workspace``). Callers are the explicit
    ``nanobot sessions migrate`` CLI command.

    Refuses to run if:
      - there is nothing to move at *source*;
      - *destination* already exists (never silently overwrites/merges);
      - *source* is currently held open by another writer (detected via
        ``_is_sqlite_db_in_use``) -- unless *force* is set. That check is
        best-effort and cannot see an *idle* open connection (see that
        function's docstring) -- stopping the process is still required.

    Sequence, deliberately **copy-then-delete, not move-then-verify-then-
    move-back**: copy the triplet (``shutil.copy2``) to *destination*, run
    ``PRAGMA integrity_check`` against the copy, and only *then* delete the
    *source* files. The original design moved the files first and rolled
    the move back on a failed check -- which meant that for the entire
    window between the move and the check (and the window it takes the
    rollback's own move to complete), no complete, verified copy of the
    data existed *anywhere*: a crash in either window could lose data
    outright, not just leave it in the "wrong" location. Copy-then-delete
    keeps the original in place, completely untouched, until an independent
    copy at the destination is verified good. Any failure to clean up
    (a partial copy after a failed copy, a leftover unverified copy after a
    failed integrity check, or the source file after a successful,
    verified migration) is logged loudly via ``logger.error`` rather than
    swallowed by ``with suppress(OSError)`` -- a silent rollback failure is
    exactly the kind of "wait, where did my history go" surprise this
    command exists to prevent, not cause.
    """
    plan = plan_session_database_migration(source, destination)
    if not plan.files:
        raise SessionDbMigrationError(f"no session database found at {plan.source}")
    if plan.destination_exists:
        raise SessionDbMigrationError(
            f"refusing to overwrite an existing database at {plan.destination}"
        )
    if plan.source_in_use and not force:
        raise SessionDbMigrationError(
            f"{plan.source} appears to be open by a running process (a write "
            "lock could not be acquired). Stop that process first, or pass "
            "--force to override (only if you are certain nothing holds it "
            "open -- moving a database out from under a live writer can "
            "corrupt it). Note this check cannot detect an *idle* open "
            "connection (no active write) -- exactly moeka.service's normal "
            "state between turns -- so passing this check is not proof the "
            "service is safe to migrate around; stop it first regardless."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    triplet = _sqlite_triplet(plan.source)
    copied: list[tuple[Path, Path]] = []

    def _cleanup_destination_copies(*, reason: str) -> None:
        for _src_path, dst_path in copied:
            try:
                dst_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.error(
                    "{}: failed to clean up the unverified copy at {}: {}. "
                    "The original at {} was never touched and remains the "
                    "source of truth -- remove the stray copy manually.",
                    reason, dst_path, cleanup_exc, plan.source,
                )

    try:
        for label, src_path in triplet:
            suffix = "" if label == "db" else f"-{label}"
            dst_path = destination.with_name(destination.name + suffix)
            shutil.copy2(str(src_path), str(dst_path))
            copied.append((src_path, dst_path))
    except OSError as exc:
        _cleanup_destination_copies(reason="failed to copy session database")
        raise SessionDbMigrationError(f"failed to copy session database: {exc}") from exc

    conn = sqlite3.connect(str(destination))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        ok = bool(result) and result[0] == "ok"
    except sqlite3.Error as exc:
        ok = False
        integrity_error = str(exc)
    else:
        integrity_error = None if ok else str(result[0] if result else "unknown")
    finally:
        conn.close()

    if not ok:
        _cleanup_destination_copies(reason="integrity check failed")
        raise SessionDbMigrationError(
            f"integrity check failed on {destination} ({integrity_error}); "
            f"the original at {plan.source} was never touched"
        )

    for src_path, _dst_path in copied:
        try:
            src_path.unlink()
        except OSError as exc:
            logger.error(
                "Session database verified and copied to {}, but removing "
                "the now-redundant source file {} failed: {}. The migration "
                "is otherwise complete and safe -- remove that file "
                "manually once you've confirmed the new location is in use.",
                destination, src_path, exc,
            )
    return plan


def get_store(session_manager: Any) -> SqliteSessionStore:
    """Return *session_manager*'s active ``SqliteSessionStore``.

    A small typed accessor for the handful of call sites that need
    SQLite-specific operations the ``SessionStore`` Protocol doesn't cover
    (``_conn()`` for a bounded raw-SQL scan, ``dump_jsonl``,
    ``export_to_workspace``). Raises if the manager wasn't constructed with
    a ``SqliteSessionStore`` — callers should not silently fall back to
    upstream's ``JsonlSessionStore`` internals, which hold none of moeka's
    actual session data.
    """
    store = session_manager._store  # noqa: SLF001 — the one sanctioned reach-in
    if not isinstance(store, SqliteSessionStore):
        raise TypeError(
            f"session_manager was not constructed with a SqliteSessionStore "
            f"(got {type(store).__name__}); this operation is moeka-specific"
        )
    return store


def _resolve_workspace_and_sessions_root(
    workspace: Path | str | None, sessions_root: Path | None,
) -> tuple[Path, Path]:
    """Shared path resolution for the two factories below.

    ``workspace`` falls back to ``get_state_home()`` (honouring the
    ``MOEKA_WORKSPACE``/``MOEKA_STATE``/``NANOBOT_HOME`` override chain)
    when a caller doesn't have one already resolved from config.
    ``sessions_root`` falls back to ``default_sessions_root()`` computed
    from that same workspace, so the two values handed to a
    ``SqliteSessionStore`` and a ``SessionManager`` constructed together
    can never drift apart — see the asymmetry risk documented in
    ``.agent/upstream-sync-handover.md`` §6.
    """
    resolved_workspace = Path(workspace).expanduser() if workspace is not None else get_state_home()
    resolved_root = (
        Path(sessions_root).expanduser()
        if sessions_root is not None
        else default_sessions_root(resolved_workspace.expanduser().resolve(strict=False))
    )
    return resolved_workspace, resolved_root


def build_sqlite_session_store(
    workspace: Path | str | None = None, *, sessions_root: Path | None = None,
) -> SqliteSessionStore:
    """Single construction seam for the default ``SqliteSessionStore`` backend.

    Consolidates what used to be three independent eager
    ``SqliteSessionStore(...)`` call sites (``nanobot/cli/commands.py``'s
    ``serve`` and ``sessions restore-workspace``, and
    ``nanobot/cli/gateway_runtime.py``'s ``_run_gateway``) behind one
    factory the test suite can mock in one place instead of three.
    """
    resolved_workspace, resolved_root = _resolve_workspace_and_sessions_root(
        workspace, sessions_root,
    )
    return SqliteSessionStore(resolved_workspace, sessions_root=resolved_root)


def build_default_session_manager(
    workspace: Path | str | None = None, *, sessions_root: Path | None = None,
) -> "SessionManager":
    """Build a ``SessionManager`` wired to the default ``SqliteSessionStore``.

    Guarantees the injected store and the ``SessionManager`` wrapper around
    it are always handed the *same* resolved ``sessions_root`` — see
    ``_resolve_workspace_and_sessions_root``. Also guarantees construction
    order: the store (and its one-time legacy-jsonl import) is fully built
    *before* ``SessionManager`` — see ``SqliteSessionStore._import_legacy_jsonl``'s
    docstring for why that order matters.

    Imports ``SessionManager`` locally (rather than at module load time) so
    this stays consistent with the test suite's existing
    ``monkeypatch.setattr("nanobot.session.manager.SessionManager", ...)``
    seam, which patches the attribute on ``nanobot.session.manager`` — a
    module-level binding here would capture the real class once at import
    time and never see that patch.
    """
    from nanobot.session.manager import SessionManager

    resolved_workspace, resolved_root = _resolve_workspace_and_sessions_root(
        workspace, sessions_root,
    )
    store = SqliteSessionStore(resolved_workspace, sessions_root=resolved_root)
    return SessionManager(resolved_workspace, sessions_root=resolved_root, store=store)
