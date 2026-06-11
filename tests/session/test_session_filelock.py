"""Cross-process write safety for SQLite-backed sessions.

The old per-file FileLock is gone; SQLite's own locking (WAL +
busy_timeout) serializes concurrent writers. This exercises the contract by
having a child process hold a write transaction on sessions.db while the
parent saves: the parent must block until the child commits, and both
writes must survive.
"""

from __future__ import annotations

import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from nanobot.session.manager import SessionManager

_IS_WINDOWS = sys.platform == "win32"
pytestmark = pytest.mark.skipif(
    _IS_WINDOWS,
    reason="cross-process subprocess fixtures are timing-flaky on Windows CI",
)


def _child_holds_write_txn_script(db_path: Path, ready_marker: Path, hold_seconds: float) -> str:
    return textwrap.dedent(
        f"""
        import sqlite3, time
        from pathlib import Path
        conn = sqlite3.connect({str(db_path)!r})
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sessions(key, created_at, updated_at, metadata,"
            " last_consolidated) VALUES ('child:1', 'x', 'x', '{{}}', 0)"
        )
        Path({str(ready_marker)!r}).write_text("ready", encoding="utf-8")
        time.sleep({hold_seconds!r})
        conn.commit()
        conn.close()
        """
    )


def test_save_blocks_while_other_process_writes(tmp_path: Path):
    import subprocess

    manager = SessionManager(workspace=tmp_path)
    session = manager.get_or_create("test:sqlite-lock")
    session.add_message("user", "hello from parent")

    ready_marker = tmp_path / "child_ready"
    hold_seconds = 1.0

    child = subprocess.Popen(
        [sys.executable, "-c", _child_holds_write_txn_script(
            manager.db_path, ready_marker, hold_seconds,
        )],
    )

    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline and not ready_marker.exists():
            time.sleep(0.02)
        assert ready_marker.exists(), "child failed to open write txn in time"

        start = time.monotonic()
        result: dict = {}

        def do_save() -> None:
            try:
                manager.save(session)
                result["ok"] = True
            except BaseException as exc:  # pragma: no cover - reported via assert
                result["err"] = exc

        t = threading.Thread(target=do_save)
        t.start()
        t.join(timeout=hold_seconds + 10.0)
        assert not t.is_alive(), "save() never returned"
        elapsed = time.monotonic() - start

        assert result.get("ok"), f"save raised: {result.get('err')!r}"
        # save() must have waited a meaningful fraction of the child's hold
        # time. Generous lower bound to absorb subprocess startup jitter.
        assert elapsed >= hold_seconds * 0.5, (
            f"save() returned in {elapsed:.3f}s; expected to block "
            f">= {hold_seconds * 0.5:.3f}s while child held the write txn"
        )
    finally:
        child.wait(timeout=10.0)

    # Both writes survived: the parent's session and the child's row.
    fresh = SessionManager(workspace=tmp_path)
    reloaded = fresh.get_or_create(session.key)
    history = reloaded.get_history(max_messages=10)
    assert any(m.get("content") == "hello from parent" for m in history)
    keys = {info["key"] for info in fresh.list_sessions()}
    assert "child:1" in keys
