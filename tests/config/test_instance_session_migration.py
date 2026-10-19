import shutil

from nanobot.config.instances import instance_uuid, migrate_sessions
from nanobot.session.manager import Session
from nanobot.session.sqlite_store import SqliteSessionStore


def test_session_migration_preserves_uuid_records_and_backup(tmp_path):
    root = tmp_path / "main"
    legacy = tmp_path / "main-sessions"
    with SqliteSessionStore(root, sessions_root=legacy) as store:
        session = Session(key="telegram:owner")
        session.add_message("user", "Remember my original history")
        store.save(session)
    original_id = instance_uuid(root)
    result = migrate_sessions(root)
    assert result["copied"] >= 2
    assert legacy.exists()
    with SqliteSessionStore(root) as store:
        assert store.load("telegram:owner").messages[0]["content"] == "Remember my original history"
    assert instance_uuid(root) == original_id
    moved = tmp_path / "moved"
    shutil.move(root, moved)
    with SqliteSessionStore(moved) as store:
        assert store.load("telegram:owner") is not None
    assert instance_uuid(moved) == original_id
    clone = tmp_path / "clone"
    shutil.copytree(moved, clone)
    with SqliteSessionStore(clone):
        assert instance_uuid(clone) != original_id
