"""WAL hygiene for the SQLite session store.

The live gateway accumulated a 5.7 MB `sessions.db-wal` against a 1.8 MB
database — far past SQLite's 1000-page auto-checkpoint threshold, which should
have capped it. The cause was `SqliteSessionStore._preview()` iterating a
cursor on the long-lived shared connection and `return`-ing from inside the
loop. SQLite holds a read transaction open for as long as an un-exhausted
statement lives, and an open read transaction blocks checkpointing.
`list_sessions()` calls `_preview()` once per session, so listing sessions
leaked one every time.

These tests assert the observable consequence — that a checkpoint can still
complete — rather than the implementation detail, so they stay meaningful if the
preview is rewritten.
"""

import sqlite3

import pytest

from nanobot.session.manager import SessionManager
from nanobot.session.sqlite_store import SqliteSessionStore, get_store


def _manager(workspace) -> SessionManager:
    """Construct a SessionManager backed by moeka's SqliteSessionStore."""
    return SessionManager(workspace, store=SqliteSessionStore(workspace))


def _seed(mgr: SessionManager, key: str, n: int = 5) -> None:
    session = mgr.get_or_create(key)
    for i in range(n):
        session.add_message("user" if i % 2 == 0 else "assistant", f"message {i}")
    mgr.save(session)


def test_checkpoint_succeeds_after_list_sessions(tmp_path) -> None:
    """The regression: listing sessions must not pin the WAL open.

    `wal_checkpoint` returns (busy, log_pages, checkpointed). A non-zero `busy`
    means a reader blocked the checkpoint — which is exactly what the abandoned
    cursor caused.
    """
    mgr = _manager(tmp_path)
    for i in range(3):
        _seed(mgr, f"telegram:{i}")

    listed = mgr.list_sessions()
    assert len(listed) == 3
    assert any(s["preview"] for s in listed), "preview should find a user message"

    store = get_store(mgr)
    busy, _log, _checkpointed = store._conn().execute(
        "PRAGMA wal_checkpoint(TRUNCATE)"
    ).fetchone()
    assert busy == 0, (
        "wal_checkpoint reported busy after list_sessions() — a reader is still "
        "open, so the WAL will grow without bound"
    )
    store.close()


def test_preview_returns_early_without_leaking(tmp_path) -> None:
    """Exercise the specific path that used to leak: an early user-role return.

    The first message is a user message, so `_preview` returns on the very first
    row with 99 more still unread — the worst case for the old code.
    """
    mgr = _manager(tmp_path)
    session = mgr.get_or_create("telegram:early")
    session.add_message("user", "first")
    for i in range(50):
        session.add_message("assistant", f"filler {i}")
    mgr.save(session)

    store = get_store(mgr)
    assert store._preview("telegram:early") == "first"

    busy, _log, _ck = store._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0, "an early return from _preview left a read transaction open"
    store.close()


def test_close_checkpoints_and_is_idempotent(tmp_path) -> None:
    mgr = _manager(tmp_path)
    store = get_store(mgr)
    _seed(mgr, "telegram:x", n=20)
    assert store._conn_obj is not None

    store.close()
    assert store._conn_obj is None
    store.close()  # must not raise

    wal = store.db_path.with_name(store.db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, (
        f"close() should truncate the WAL, found {wal.stat().st_size} bytes"
    )

    # The data is still there and readable by an independent connection.
    con = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM messages WHERE session_key = ?", ("telegram:x",)
        ).fetchone()[0] == 20
    finally:
        con.close()


def test_context_manager_closes(tmp_path) -> None:
    """The store itself (not the SessionManager wrapper) supports `with`."""
    with SqliteSessionStore(tmp_path) as store:
        mgr = SessionManager(tmp_path, store=store)
        _seed(mgr, "telegram:ctx")
        assert store._conn_obj is not None
    assert store._conn_obj is None


@pytest.mark.parametrize("pragma,expected", [("journal_mode", "wal"), ("busy_timeout", 10000)])
def test_vec_store_connection_pragmas(tmp_path, pragma, expected) -> None:
    """VecStore opened with no pragmas at all, unlike the session store next door.

    Without busy_timeout a concurrent writer raises "database is locked"
    immediately, and every query in VecStore is wrapped in a catch-all that
    returns [] — so a locked database looked identical to "no results".
    """
    from nanobot.core.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    try:
        value = store._connection().execute(f"PRAGMA {pragma}").fetchone()[0]
        assert (value.lower() if isinstance(value, str) else value) == expected
    finally:
        store.close()
