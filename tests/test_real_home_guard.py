"""Proof that the root conftest.py real-$HOME guard intercepts the specific
operations that caused the 2026-09-17 incident, not just directory creation.

The incident itself was a ``shutil.move`` of an existing file into an
already-existing real ``~/.nanobot`` -- no directory was created, so the
original mkdir-only guard would not have caught it. This file exercises each
guarded operation directly against a hardcoded real-home path and asserts it
raises *before* touching the filesystem, so a future edit that narrows or
removes coverage fails loudly here instead of silently reopening the hole.

Uses ``tests/_home_guard.py``'s constants (the real, pre-redirect ``$HOME``)
rather than anything derived from the ``$HOME`` this test process currently
sees -- by the time this test runs, ``$HOME`` has already been redirected to
an ephemeral test-session directory by the root conftest's
``pytest_configure``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from tests._home_guard import REAL_NANOBOT_HOME, REAL_NANOBOT_SESSIONS_HOME

# A path that resolves under the real ~/.nanobot but is never actually
# created/touched -- every guarded call below must raise before any I/O
# happens, so this file's non-existence is itself part of the proof: if any
# of these tests somehow left it behind, that would mean the guard didn't
# fire before the real operation ran.
_LEAK_TARGET = REAL_NANOBOT_HOME / "test-real-home-guard-leak-canary"


@pytest.fixture(autouse=True)
def _assert_no_leak_survives():
    assert not _LEAK_TARGET.exists(), (
        "a previous test in this file left a real artifact behind -- the "
        "guard did not raise before the underlying operation ran"
    )
    yield
    assert not _LEAK_TARGET.exists(), (
        "this test's guarded operation reached the real filesystem instead "
        "of being intercepted"
    )


def test_guards_cover_both_locked_real_home_paths():
    """Sanity-check the fixture constants before trusting the tests below."""
    assert REAL_NANOBOT_HOME.name == ".nanobot"
    assert REAL_NANOBOT_SESSIONS_HOME.name == ".nanobot-sessions"


def test_path_mkdir_raises_under_real_home():
    with pytest.raises(AssertionError, match="TEST LEAK"):
        _LEAK_TARGET.mkdir()


def test_os_makedirs_raises_under_real_home():
    with pytest.raises(AssertionError, match="TEST LEAK"):
        os.makedirs(str(_LEAK_TARGET))


def test_os_mkdir_raises_under_real_home():
    with pytest.raises(AssertionError, match="TEST LEAK"):
        os.mkdir(str(_LEAK_TARGET))


def test_shutil_move_raises_when_destination_is_under_real_home(tmp_path: Path):
    source = tmp_path / "harmless.txt"
    source.write_text("not real data", encoding="utf-8")
    with pytest.raises(AssertionError, match="TEST LEAK"):
        shutil.move(str(source), str(_LEAK_TARGET))
    # The guard must have fired before shutil.move touched anything.
    assert source.exists()


def test_shutil_move_raises_when_source_is_under_real_home(tmp_path: Path):
    with pytest.raises(AssertionError, match="TEST LEAK"):
        shutil.move(str(_LEAK_TARGET), str(tmp_path / "exfiltrated.txt"))
    assert not (tmp_path / "exfiltrated.txt").exists()


def test_os_rename_raises_under_real_home(tmp_path: Path):
    source = tmp_path / "harmless.txt"
    source.write_text("not real data", encoding="utf-8")
    with pytest.raises(AssertionError, match="TEST LEAK"):
        os.rename(str(source), str(_LEAK_TARGET))
    assert source.exists()


def test_os_replace_raises_under_real_home(tmp_path: Path):
    source = tmp_path / "harmless.txt"
    source.write_text("not real data", encoding="utf-8")
    with pytest.raises(AssertionError, match="TEST LEAK"):
        os.replace(str(source), str(_LEAK_TARGET))
    assert source.exists()


def test_sqlite3_connect_raises_under_real_home():
    with pytest.raises(AssertionError, match="TEST LEAK"):
        sqlite3.connect(str(_LEAK_TARGET / "sessions.db"))


def test_sessions_sibling_root_is_guarded_too():
    """The default_sessions_root() sibling (~/.nanobot-sessions) is guarded
    identically -- ADR-0001 sessions live there, not inside ~/.nanobot."""
    target = REAL_NANOBOT_SESSIONS_HOME / "test-real-home-guard-leak-canary"
    with pytest.raises(AssertionError, match="TEST LEAK"):
        target.mkdir()
    with pytest.raises(AssertionError, match="TEST LEAK"):
        sqlite3.connect(str(target / "sessions.db"))


def test_unrelated_real_home_paths_are_not_guarded(tmp_path: Path):
    """The guard is scoped to nanobot/moeka state, not the whole real $HOME --
    a test creating e.g. its own tmp_path tree must not be affected."""
    scratch = tmp_path / "unrelated" / "nested"
    scratch.mkdir(parents=True)
    assert scratch.is_dir()
