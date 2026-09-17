"""Tests for durable session saves (WAL checkpoint) and flush_all on shutdown."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import call, patch

import pytest

from nanobot.session.manager import SessionManager
from nanobot.session.sqlite_store import SqliteSessionStore, get_store


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return tmp_path


@pytest.fixture
def manager(sessions_dir: Path) -> SessionManager:
    return SessionManager(
        workspace=sessions_dir,
        store=SqliteSessionStore(workspace=sessions_dir),
    )


class TestSaveDurability:
    """save(fsync=True) checkpoints the WAL so writes hit the main db file."""

    def test_save_with_fsync_truncates_wal(self, manager: SessionManager):
        session = manager.get_or_create("test:with-fsync")
        session.add_message("user", "hello")
        manager.save(session, fsync=True)

        wal = Path(str(get_store(manager).db_path) + "-wal")
        # TRUNCATE checkpoint leaves an empty (or absent) WAL.
        assert not wal.exists() or os.path.getsize(wal) == 0

    def test_save_without_fsync_leaves_wal_pending(self, manager: SessionManager):
        session = manager.get_or_create("test:no-fsync")
        session.add_message("user", "hello")
        manager.save(session, fsync=False)

        wal = Path(str(get_store(manager).db_path) + "-wal")
        assert wal.exists() and os.path.getsize(wal) > 0

    def test_save_default_no_checkpoint(self, manager: SessionManager):
        """Default save() skips the checkpoint (backward compat with fsync=False)."""
        session = manager.get_or_create("test:default")
        session.add_message("user", "hello")
        manager.save(session)
        wal = Path(str(get_store(manager).db_path) + "-wal")
        assert wal.exists() and os.path.getsize(wal) > 0


class TestJsonlStoreDirectoryFsync:
    """upstream's manager.py (taken wholesale) still fsyncs the sessions
    directory itself on a durable save through the always-real, internal
    ``JsonlSessionStore``; moeka's live path never persists through it (it
    passes its own SqliteSessionStore), but the fallback behavior below is
    still real, reachable code -- exercise it against a bare, jsonl-backed
    SessionManager rather than the SQLite fixture above.
    """

    @pytest.fixture
    def jsonl_manager(self, sessions_dir: Path) -> SessionManager:
        return SessionManager(workspace=sessions_dir)

    def test_save_ignores_unsupported_directory_fsync(
        self, jsonl_manager: SessionManager
    ) -> None:
        """Shared filesystems may open directories but reject directory fsync."""
        session = jsonl_manager.get_or_create("test:unsupported-directory-fsync")
        session.add_message("user", "hello")
        directory_fd = 987654
        with (
            jsonl_manager.locked_session_files(),
            patch("nanobot.session.manager.os.open", return_value=directory_fd) as open_dir,
            patch(
                "nanobot.session.manager.os.fsync",
                side_effect=[None, OSError(errno.EINVAL, "Invalid argument")],
            ),
            patch("nanobot.session.manager.os.close") as close_dir,
        ):
            jsonl_manager.save(session, fsync=True)

        assert jsonl_manager._get_session_path(session.key).exists()
        open_dir.assert_called_once_with(str(jsonl_manager.sessions_dir), os.O_RDONLY)
        assert close_dir.call_args_list.count(call(directory_fd)) == 1

    def test_save_propagates_other_directory_fsync_errors(
        self, jsonl_manager: SessionManager
    ) -> None:
        """Only EINVAL is an expected unsupported-directory-fsync result."""
        session = jsonl_manager.get_or_create("test:directory-fsync-io-error")
        directory_fd = 987654
        with (
            jsonl_manager.locked_session_files(),
            patch("nanobot.session.manager.os.open", return_value=directory_fd),
            patch(
                "nanobot.session.manager.os.fsync",
                side_effect=[None, OSError(errno.EIO, "I/O error")],
            ),
            patch("nanobot.session.manager.os.close") as close_dir,
            pytest.raises(OSError, match="I/O error"),
        ):
            jsonl_manager.save(session, fsync=True)

        assert close_dir.call_args_list.count(call(directory_fd)) == 1


class TestFlushAll:
    """Verify flush_all re-saves all cached sessions durably."""

    def test_flush_all_empty_cache(self, manager: SessionManager):
        assert manager.flush_all() == 0

    def test_flush_all_saves_cached_sessions(self, manager: SessionManager):
        s1 = manager.get_or_create("test:session-1")
        s1.add_message("user", "msg 1")
        manager.save(s1)

        s2 = manager.get_or_create("test:session-2")
        s2.add_message("user", "msg 2")
        manager.save(s2)

        flushed = manager.flush_all()
        assert flushed == 2

    def test_flush_all_checkpoints_wal(self, manager: SessionManager):
        session = manager.get_or_create("test:fsync-check")
        session.add_message("user", "important")
        manager.save(session)

        manager.flush_all()
        wal = Path(str(get_store(manager).db_path) + "-wal")
        assert not wal.exists() or os.path.getsize(wal) == 0

    def test_flush_all_continues_on_error(self, manager: SessionManager):
        """One broken session should not prevent others from flushing."""
        s1 = manager.get_or_create("test:good")
        s1.add_message("user", "ok")
        manager.save(s1)

        s2 = manager.get_or_create("test:bad")
        s2.add_message("user", "ok")
        manager.save(s2)

        original_save = manager.save
        call_count = {"n": 0}

        def patched_save(session, *, fsync=False):
            call_count["n"] += 1
            if session.key == "test:bad":
                raise OSError("disk on fire")
            original_save(session, fsync=fsync)

        manager.save = patched_save
        flushed = manager.flush_all()

        # One succeeded, one failed — flush_all returns successful count
        assert flushed == 1
        assert call_count["n"] == 2

    def test_flush_all_data_survives_reload(self, sessions_dir: Path):
        """Data flushed by flush_all should survive a fresh SessionManager load."""
        mgr1 = SessionManager(
            workspace=sessions_dir,
            store=SqliteSessionStore(workspace=sessions_dir),
        )
        session = mgr1.get_or_create("test:persist")
        session.add_message("user", "remember this")
        session.add_message("assistant", "noted")
        mgr1.save(session)
        mgr1.flush_all()

        # Simulate process restart — new manager, cold cache
        mgr2 = SessionManager(
            workspace=sessions_dir,
            store=SqliteSessionStore(workspace=sessions_dir),
        )
        reloaded = mgr2.get_or_create("test:persist")
        history = reloaded.get_history(max_messages=100)

        assert len(history) == 2
        assert history[0]["content"] == "remember this"
        assert history[1]["content"] == "noted"

    # NOTE: upstream also has a TestLoadErrors class asserting that
    # PermissionError during file I/O is not swallowed as corrupt data. That
    # doesn't map cleanly onto the SQLite store: reads/writes go through
    # sqlite3, not builtins.open, and _SESSION_DATA_ERRORS deliberately
    # catches sqlite3.Error broadly (matching moeka's original defensive
    # "never crash on load" posture for _load/list_sessions/etc.) rather than
    # distinguishing access-denied from corrupt-data failures. Flagged for a
    # human to decide whether SQLite-level permission errors should propagate
    # instead of being swallowed.
