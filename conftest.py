"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import shutil
import sqlite3
import ssl
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import certifi
import pytest
from loguru import logger

from tests._home_guard import path_is_guarded

# --- Real-$HOME isolation --------------------------------------------------
#
# Incident (2026-09-17): a code path under test resolved a workspace to the
# real ~/.nanobot and triggered SqliteSessionStore's out-of-workspace
# migration against the live moeka.service's actual session database,
# relocating it to ~/.nanobot-sessions/<id>/. No data was lost (the running
# process kept its open file descriptor; SQLite files were moved back), but
# a bare per-test "remember to pass tmp_path" convention is not a structural
# guarantee. This block makes it structurally impossible for a test process
# to resolve *any* path under the real $HOME, for the whole pytest session,
# regardless of whether an individual test remembers to isolate itself:
#   1. $HOME (and Path.home()) is redirected to an ephemeral directory for
#      every test in this process, before any test collection happens.
#   2. Path.mkdir / os.makedirs / os.mkdir are wrapped for the same
#      duration: any attempt to create a directory under the *real*,
#      pre-redirect $HOME raises immediately, loudly, with the offending
#      path -- a second, independent guard in case some code resolves a
#      path via a route that doesn't go through Path.home() (e.g. a cached
#      value, or get_state_home()'s MOEKA_WORKSPACE/NANOBOT_HOME env
#      overrides, which this fixture also clears).
#   3. shutil.move / os.rename / os.replace / sqlite3.connect are wrapped
#      too (2026-09-17 review): the mkdir guard only ever caught directory
#      *creation*. The incident itself was a file move -- shutil.move'ing
#      an existing sessions.db out of an already-existing real ~/.nanobot --
#      which never calls mkdir at all and was never actually intercepted by
#      this guard. sqlite3.connect is guarded too since SQLite creates the
#      database file itself on first connect, another path that touches the
#      real home without ever calling mkdir.
#
# The real-home constants these checks compare against live in
# tests/_home_guard.py, not here: they must be computed once, before the
# $HOME redirect below runs, and tests/conftest.py's own guard needs the
# same values -- see that module's docstring for why a shared module (not a
# recompute-from-Path.home() in each file) is what makes both guards agree.


def _reject_if_under_real_home(path: object, *, op: str) -> None:
    guarded = path_is_guarded(path)
    if guarded is not None:
        raise AssertionError(
            f"TEST LEAK: attempted to {op} under the real nanobot/moeka "
            f"state home ({guarded}): {path!r}. Tests must not resolve "
            f"paths under the real home -- use tmp_path (or pass an "
            f"explicit workspace/sessions_root) instead."
        )


def _install_real_home_guards() -> None:
    import pathlib

    original_path_mkdir = pathlib.Path.mkdir
    original_os_makedirs = os.makedirs
    original_os_mkdir = os.mkdir
    original_shutil_move = shutil.move
    original_os_rename = os.rename
    original_os_replace = os.replace
    original_sqlite_connect = sqlite3.connect

    def guarded_path_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(self, op="create a directory")
        return original_path_mkdir(self, *args, **kwargs)

    def guarded_os_makedirs(name: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(name, op="create a directory")
        return original_os_makedirs(name, *args, **kwargs)

    def guarded_os_mkdir(path: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(path, op="create a directory")
        return original_os_mkdir(path, *args, **kwargs)

    def guarded_shutil_move(src: object, dst: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(src, op="move a file/directory from")
        _reject_if_under_real_home(dst, op="move a file/directory to")
        return original_shutil_move(src, dst, *args, **kwargs)

    def guarded_os_rename(src: object, dst: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(src, op="rename from")
        _reject_if_under_real_home(dst, op="rename to")
        return original_os_rename(src, dst, *args, **kwargs)

    def guarded_os_replace(src: object, dst: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(src, op="replace from")
        _reject_if_under_real_home(dst, op="replace to")
        return original_os_replace(src, dst, *args, **kwargs)

    def guarded_sqlite_connect(database: object, *args: object, **kwargs: object) -> object:
        # database may be a str/Path/os.PathLike, or ":memory:"/a URI --
        # path_is_guarded() already tolerates anything Path() can't parse.
        _reject_if_under_real_home(database, op="open a sqlite3 connection to")
        return original_sqlite_connect(database, *args, **kwargs)

    pathlib.Path.mkdir = guarded_path_mkdir  # type: ignore[method-assign]
    os.makedirs = guarded_os_makedirs
    os.mkdir = guarded_os_mkdir
    shutil.move = guarded_shutil_move
    os.rename = guarded_os_rename
    os.replace = guarded_os_replace
    sqlite3.connect = guarded_sqlite_connect  # type: ignore[assignment]


def pytest_configure(config: pytest.Config) -> None:
    """Redirect $HOME for the whole test session before any test runs.

    Runs at pytest startup, before collection -- earlier than any fixture
    could -- so no test (or module-level code executed at import/collection
    time) can observe the real $HOME.
    """
    fake_home = tempfile.mkdtemp(prefix="nanobot-test-home-")
    os.environ["HOME"] = fake_home
    os.environ["USERPROFILE"] = fake_home  # Windows equivalent
    # get_state_home() also honours these; clear so a leaked real value from
    # the invoking shell can't override the $HOME redirection above.
    os.environ.pop("MOEKA_WORKSPACE", None)
    os.environ.pop("MOEKA_STATE", None)
    os.environ.pop("NANOBOT_HOME", None)
    _install_real_home_guards()


@pytest.fixture(autouse=True)
def _isolate_nanobot_log_activation() -> Iterator[None]:
    """Keep CLI log settings from leaking into later tests in the same process."""
    logger.enable("nanobot")
    try:
        yield
    finally:
        logger.enable("nanobot")


@pytest.fixture(autouse=True)
def _isolate_sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect session storage away from the real active config data directory.

    Session storage lives under the active runtime data root (outside the workspace,
    per ADR-0001), so without redirection tests would write into the real home.
    """
    runtime_root = tmp_path.parent / f"{tmp_path.name}-runtime-root"
    legacy_root = tmp_path.parent / f"{tmp_path.name}-legacy-sessions-root"

    def runtime_subdir(name: str) -> Path:
        path = runtime_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Both session stores reach the legacy runtime/global-session dirs only through
    # the ambient adapter's helpers (lazily imported at call time), so patching
    # them there covers nanobot.session.manager and nanobot.session.sqlite_store.
    # SqliteSessionStore never calls legacy_runtime_subdir (its default root is
    # workspace-relative, see default_sessions_root()); legacy_sessions_dir is
    # resolved on use only.
    monkeypatch.setattr(
        "nanobot.kernel.legacy.legacy_runtime_subdir",
        runtime_subdir,
    )
    monkeypatch.setattr(
        "nanobot.kernel.legacy.legacy_sessions_dir",
        lambda: legacy_root,
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_pairing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep channel pairing tests out of the user's active pairing store (gateway host)."""
    pairing_path = tmp_path / "pairing.json"
    monkeypatch.setattr(
        "nanobot.pairing.store._store_path",
        lambda: pairing_path,
    )


@pytest.fixture(scope="session", autouse=True)
def _use_windows_system_ca_for_default_http_clients() -> Iterator[None]:
    """Avoid reparsing certifi's CA bundle for every offline HTTP client.

    Loading certifi takes roughly 0.7 seconds per client on Windows. The test
    suite constructs hundreds of clients while mocking their I/O. System roots
    preserve certificate verification for accidental local requests; explicit
    ``cafile``, ``capath``, and ``cadata`` arguments still use the real loader.
    """
    if sys.platform != "win32":
        yield
        return

    original = ssl.create_default_context
    certifi_path = os.path.normcase(os.path.abspath(certifi.where()))

    def create_default_context(
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        requested_path = os.path.normcase(os.path.abspath(cafile)) if cafile else None
        if requested_path == certifi_path and capath is None and cadata is None:
            return original(purpose)
        return original(
            purpose,
            cafile=cafile,
            capath=capath,
            cadata=cadata,
        )

    ssl.create_default_context = create_default_context
    try:
        yield
    finally:
        ssl.create_default_context = original
