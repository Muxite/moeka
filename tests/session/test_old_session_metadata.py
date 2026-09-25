"""Sessions saved before the core slim (with WebUI metadata) still load and save.

The core no longer interprets WebUI turn metadata, but existing ``sessions.db``
rows carry it.  Session metadata is opaque to the store: it must round-trip
verbatim through a fresh ``SessionManager`` (i.e. through the store, not the
per-instance cache) and survive a further load-modify-save cycle.  The SQLite
store is what ``AgentLoop`` wires up at runtime; the JSONL default is covered
too.
"""

from __future__ import annotations

import copy

import pytest

from nanobot.session.manager import SessionManager
from nanobot.session.sqlite_store import SqliteSessionStore

# Literal keys as written by the pre-slim WebUI (``WEBUI_TURN_METADATA_KEY`` was
# "webui_turn_id"; recovery state lived under "webui_recovery").
_LEGACY_METADATA = {
    "webui": True,
    "webui_turn_id": "t1",
    "webui_turn": {"turn_id": "t1"},
    "title": "Legacy chat",
    "title_user_edited": True,
    "webui_recovery": {
        "status": "recovered",
        "recovery_id": "abc123",
        "attempts": 0,
        "reason": "dismissed",
        "updated_at": "2026-09-01T00:00:00",
    },
}


def _open(workspace, backend):
    if backend == "sqlite":
        store = SqliteSessionStore(workspace)
        return SessionManager(workspace, store=store), store
    return SessionManager(workspace), None


@pytest.mark.parametrize("backend", ["sqlite", "jsonl"])
@pytest.mark.parametrize("key", ["websocket:legacy", "sdk:legacy"])
def test_session_with_webui_metadata_roundtrips(tmp_path, key, backend):
    stores = []

    def manager():
        mgr, store = _open(tmp_path, backend)
        if store is not None:
            stores.append(store)
        return mgr

    try:
        mgr = manager()
        sess = mgr.get_or_create(key)
        sess.metadata.update(copy.deepcopy(_LEGACY_METADATA))
        sess.add_message("user", "hi")
        mgr.save(sess)
        if backend == "sqlite":
            assert stores[0].db_path.is_file()
            assert tmp_path not in stores[0].db_path.parents  # sibling dir, not in workspace

        again = manager().get_or_create(key)
        assert again.messages[-1]["content"] == "hi"
        for name, value in _LEGACY_METADATA.items():
            assert again.metadata[name] == value

        # Loaded legacy sessions keep saving normally, metadata untouched.
        again.add_message("assistant", "hello")
        manager().save(again)
        third = manager().get_or_create(key)
        assert [m["content"] for m in third.messages[-2:]] == ["hi", "hello"]
        for name, value in _LEGACY_METADATA.items():
            assert third.metadata[name] == value
    finally:
        for store in stores:
            store.close()
