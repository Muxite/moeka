"""Regression tests for session key collision-safety.

Upstream's per-file jsonl store needed a collision-resistant filename
encoding (``_storage_key``) because ``safe_key()`` lossily collapses distinct
keys (e.g. "a_b" and "a:b" both sanitize to the same filename stem). moeka's
SQLite store sidesteps that whole problem: ``key`` is the table's TEXT PRIMARY
KEY, used verbatim, so lossily-colliding ``safe_key()`` stems can never
collide in storage. These tests pin that guarantee (and the part of
``safe_key()`` itself that is still relied on elsewhere, e.g. WebUI activity
file naming).
"""

from pathlib import Path

from nanobot.session.manager import Session, SessionManager


def test_safe_key_is_lossy() -> None:
    """safe_key() is a display/legacy-path helper, not a storage key — it may
    collapse distinct session keys to the same filename stem."""
    assert SessionManager.safe_key("telegram:a_b") == SessionManager.safe_key("telegram:a:b")


def test_sqlite_keys_never_collide_even_when_safe_key_does(tmp_path: Path) -> None:
    """Two keys that lossily collide under safe_key() must still be stored
    and loaded as fully distinct sessions in the SQLite store."""
    sm = SessionManager(tmp_path)
    first = Session(key="telegram:a_b")
    first.add_message("user", "underscore history")
    second = Session(key="telegram:a:b")
    second.add_message("user", "colon history")

    sm.save(first)
    sm.save(second)

    assert sm.safe_key(first.key) == sm.safe_key(second.key)

    sm.invalidate(first.key)
    sm.invalidate(second.key)
    loaded_first = sm._load(first.key)
    loaded_second = sm._load(second.key)

    assert loaded_first is not None
    assert loaded_second is not None
    assert loaded_first.messages[0]["content"] == "underscore history"
    assert loaded_second.messages[0]["content"] == "colon history"

    # Deleting one must not affect the other.
    assert sm.delete_session(first.key) is True
    assert sm.read_session_file(first.key) is None
    assert sm.read_session_file(second.key) is not None
