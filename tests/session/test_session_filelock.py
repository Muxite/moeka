"""Tests for the moeka deviation: cross-process FileLock on session save.

CLAUDE.md flags ``nanobot/session/manager.py`` ``save()`` as wrapped in a
``FileLock`` so a stray second moeka process (e.g. one launched by hand
alongside the systemd unit) can't clobber recent appends.

This test exercises the contract by spawning a child process that holds
the lock for ~1 second, then asserts the parent's ``save()`` blocks for
roughly that long before completing, and that the resulting file reloads
cleanly.
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


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def manager(workspace: Path) -> SessionManager:
    return SessionManager(workspace=workspace)


def _child_holds_lock_script(lock_path: Path, ready_marker: Path, hold_seconds: float) -> str:
    return textwrap.dedent(
        f"""
        from pathlib import Path
        from filelock import FileLock
        lock = FileLock({str(lock_path)!r})
        with lock:
            Path({str(ready_marker)!r}).write_text("ready", encoding="utf-8")
            import time
            time.sleep({hold_seconds!r})
        """
    )


def test_save_blocks_while_other_process_holds_lock(
    manager: SessionManager, workspace: Path, tmp_path: Path
):
    import subprocess

    session = manager.get_or_create("test:filelock")
    session.add_message("user", "hello from parent")

    # Pre-create the session file path so the lock file is at a known location.
    session_path = manager._get_session_path(session.key)
    lock_path = Path(str(session_path) + ".lock")
    session_path.parent.mkdir(parents=True, exist_ok=True)

    ready_marker = tmp_path / "child_ready"
    hold_seconds = 1.0

    child = subprocess.Popen(
        [sys.executable, "-c", _child_holds_lock_script(lock_path, ready_marker, hold_seconds)],
    )

    # Wait until the child confirms it has acquired the lock.
    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline and not ready_marker.exists():
            time.sleep(0.02)
        assert ready_marker.exists(), "child failed to acquire lock in time"

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
        t.join(timeout=hold_seconds + 5.0)
        assert not t.is_alive(), "save() never returned"
        elapsed = time.monotonic() - start

        assert result.get("ok"), f"save raised: {result.get('err')!r}"
        # save() must have waited a meaningful fraction of the child's hold
        # time. Generous lower bound to absorb subprocess startup jitter.
        assert elapsed >= hold_seconds * 0.5, (
            f"save() returned in {elapsed:.3f}s; expected to block "
            f">= {hold_seconds * 0.5:.3f}s while child held lock"
        )
    finally:
        child.wait(timeout=10.0)

    # The persisted session must reload cleanly.
    fresh = SessionManager(workspace=workspace)
    reloaded = fresh.get_or_create(session.key)
    history = reloaded.get_history(max_messages=10)
    assert any(m.get("content") == "hello from parent" for m in history)
