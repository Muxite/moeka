"""Tests for the explicit, opt-in sessions.db migration path (not startup auto-move).

Covers the 2026-09-17 incident fix: SqliteSessionStore.__init__ must never move a
legacy in-workspace sessions.db on its own (it may only warn), and the only way
to actually relocate a database is nanobot.session.sqlite_store.migrate_session_database
/ the `nanobot sessions migrate` CLI command, both of which default to dry-run,
refuse to overwrite an existing destination, verify destination integrity before
declaring success, and refuse (absent --force) to move a database that looks
currently open by another process.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanobot.cli import commands
from nanobot.session.sqlite_store import (
    SessionDbMigrationError,
    SqliteSessionStore,
    migrate_session_database,
    plan_session_database_migration,
)


def _make_sqlite_db(path: Path, *, wal: bool = False, shm: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL" if wal else "PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO sessions VALUES ('x')")
    conn.commit()
    conn.close()
    if shm and not path.with_name(path.name + "-shm").exists():
        path.with_name(path.name + "-shm").write_bytes(b"")


def test_startup_never_moves_a_legacy_in_workspace_db(tmp_path: Path) -> None:
    """Constructing a store must warn, not move, a pre-ADR-0001 in-workspace db."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_db = workspace / "sessions.db"
    _make_sqlite_db(legacy_db)

    sessions_root = tmp_path / "sessions-root"
    store = SqliteSessionStore(workspace, sessions_root=sessions_root)

    # Legacy file is untouched; the new store created its own, separate db.
    assert legacy_db.exists()
    assert store.db_path != legacy_db
    assert store.db_path.exists()


def test_plan_reports_files_without_moving_anything(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source, wal=True, shm=True)
    destination = tmp_path / "dst" / "sessions.db"

    plan = plan_session_database_migration(source, destination)

    assert source.exists()  # nothing moved
    assert {label for label, _size in plan.files} >= {"db"}
    assert plan.destination_exists is False
    assert plan.source_in_use is False


def test_migrate_moves_triplet_and_verifies_destination(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    migrate_session_database(source, destination)

    assert not source.exists()
    assert destination.exists()
    conn = sqlite3.connect(str(destination))
    try:
        assert conn.execute("SELECT key FROM sessions").fetchone() == ("x",)
    finally:
        conn.close()


def test_migrate_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"
    _make_sqlite_db(destination)

    with pytest.raises(SessionDbMigrationError, match="refusing to overwrite"):
        migrate_session_database(source, destination)

    # Nothing moved on refusal.
    assert source.exists()
    assert destination.exists()


def test_migrate_refuses_when_source_appears_open(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    holder = sqlite3.connect(str(source))
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(SessionDbMigrationError, match="open by a running process"):
            migrate_session_database(source, destination)
        assert source.exists()
        assert not destination.exists()
    finally:
        holder.rollback()
        holder.close()


def test_migrate_force_overrides_the_in_use_check(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    holder = sqlite3.connect(str(source))
    try:
        holder.execute("BEGIN IMMEDIATE")
        # Lock is held by `holder` for the whole call below -- on POSIX a
        # file rename doesn't require the lock, so force=True can move the
        # triplet out from under it (this is exactly why --force is
        # dangerous and documented as such: the writer keeps its file
        # descriptor pointed at the old inode, not the new path).
        migrate_session_database(source, destination, force=True)
        assert destination.exists()
        assert not source.exists()
    finally:
        # Best-effort cleanup: the holder's fd is still valid (POSIX rename
        # doesn't invalidate open descriptors), so this should succeed, but
        # is not itself part of what the test is asserting.
        try:
            holder.rollback()
        except sqlite3.Error:
            pass
        holder.close()


def test_migrate_rolls_back_on_destination_integrity_failure(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"
    destination.parent.mkdir(parents=True)

    from unittest.mock import patch

    class _FakeCursor:
        def fetchone(self) -> tuple[str]:
            return ("corruption found",)

    class _FakeConn:
        def execute(self, *_args: object, **_kwargs: object) -> _FakeCursor:
            return _FakeCursor()

        def close(self) -> None:
            return None

    with patch("nanobot.session.sqlite_store.sqlite3.connect", return_value=_FakeConn()):
        with pytest.raises(SessionDbMigrationError, match="integrity check failed"):
            migrate_session_database(source, destination)

    # Rolled back: source restored, destination cleaned up.
    assert source.exists()
    assert not destination.exists()


def test_migrate_cli_defaults_to_dry_run_and_moves_nothing(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    result = CliRunner().invoke(
        commands.app,
        ["sessions", "migrate", "--from", str(source), "--to", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    assert source.exists()
    assert not destination.exists()


def test_migrate_cli_requires_both_no_dry_run_and_yes(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    # --yes alone (still dry-run by default) must not move anything.
    result = CliRunner().invoke(
        commands.app,
        ["sessions", "migrate", "--from", str(source), "--to", str(destination), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert source.exists()
    assert not destination.exists()


def test_migrate_cli_executes_with_no_dry_run_and_yes(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sessions.db"
    _make_sqlite_db(source)
    destination = tmp_path / "dst" / "sessions.db"

    result = CliRunner().invoke(
        commands.app,
        [
            "sessions", "migrate",
            "--from", str(source), "--to", str(destination),
            "--no-dry-run", "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Migrated" in result.output
    assert not source.exists()
    assert destination.exists()
