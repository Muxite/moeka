"""Spec 005 FR-033..FR-036: the per-state-dir instance lock."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from nanobot.kernel import instance_lock as il
from nanobot.kernel.instance_lock import InstanceLockedError, acquire_instance_lock


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*")) if p.is_file()
    }


def test_acquire_writes_holder_record_and_release_frees(tmp_path: Path) -> None:
    state = tmp_path / "state"
    lock = acquire_instance_lock(state)
    holder = json.loads((state / ".instance.json").read_text())
    assert set(holder) >= {"pid", "hostname", "started_at", "argv0", "mode"}
    assert holder["pid"] == os.getpid() and holder["mode"] == "write"
    assert holder["started_at"].endswith("Z")
    assert (state / ".instance.lock").exists()
    lock.release()
    lock.release()  # idempotent
    acquire_instance_lock(state).release()


def test_second_writer_in_process_refused_also_via_symlink(tmp_path: Path) -> None:
    state = tmp_path / "state"
    link = tmp_path / "link"
    with acquire_instance_lock(state):
        link.symlink_to(state)
        before = _tree(state)
        for target in (state, link):
            started = time.monotonic()
            with pytest.raises(InstanceLockedError) as info:
                acquire_instance_lock(target)
            assert time.monotonic() - started < 2
            err = info.value
            assert err.reason == "held"
            assert err.state_dir == state.resolve()
            assert str(state.resolve()) in str(err) and f"pid {os.getpid()}" in str(err)
            assert err.holder is not None and err.holder["pid"] == os.getpid()
        assert _tree(state) == before


_HOLDER = textwrap.dedent("""
    import sys, time
    from nanobot.kernel.instance_lock import acquire_instance_lock
    lock = acquire_instance_lock(sys.argv[1])
    print("locked", flush=True)
    time.sleep(120)
""")


def _holder_process(state: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(state)], stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_cross_process_refusal_and_sigkill_release(tmp_path: Path) -> None:
    state = tmp_path / "state"
    proc = _holder_process(state)
    try:
        before = _tree(state)
        with pytest.raises(InstanceLockedError) as info:
            acquire_instance_lock(state)
        assert f"pid {proc.pid}" in str(info.value)
        assert info.value.holder["pid"] == proc.pid
        assert _tree(state) == before
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        deadline = time.monotonic() + 2
        while True:
            try:
                lock = acquire_instance_lock(state)
                break
            except InstanceLockedError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        assert json.loads((state / ".instance.json").read_text())["pid"] == os.getpid()
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lock_unsupported(tmp_path: Path, monkeypatch) -> None:
    def no_flock(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(il.fcntl, "flock", no_flock)
    with pytest.raises(InstanceLockedError) as info:
        acquire_instance_lock(tmp_path / "s")
    assert info.value.reason == "lock_unsupported"


def test_lock_fd_is_not_inherited_by_exec(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with acquire_instance_lock(state):
        child = subprocess.Popen(["sleep", "30"])
    try:
        # The child survives the holder's release; it must not keep the lock.
        acquire_instance_lock(state).release()
    finally:
        child.kill()
        child.wait()
