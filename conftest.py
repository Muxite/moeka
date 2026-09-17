"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import ssl
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import certifi
import pytest
from loguru import logger

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
_REAL_HOME = Path(os.environ.get("HOME") or os.path.expanduser("~")).resolve()
# Guard specific nanobot/moeka state locations under the real $HOME, not the
# whole home directory -- the project checkout, .cache, .pytest_cache, etc.
# all legitimately live under $HOME too and must stay writable.
_REAL_NANOBOT_HOME = _REAL_HOME / ".nanobot"
_GUARDED_REAL_PATHS = (
    _REAL_NANOBOT_HOME,
    _REAL_HOME / f"{_REAL_NANOBOT_HOME.name}-sessions",  # default_sessions_root()'s sibling
)


def _install_real_home_mkdir_guard() -> None:
    import pathlib

    original_path_mkdir = pathlib.Path.mkdir
    original_os_makedirs = os.makedirs
    original_os_mkdir = os.mkdir

    def _reject_if_under_real_home(path: object) -> None:
        try:
            resolved = Path(os.fspath(path)).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError):
            return
        for guarded in _GUARDED_REAL_PATHS:
            if resolved == guarded or resolved.is_relative_to(guarded):
                raise AssertionError(
                    f"TEST LEAK: attempted to create a directory under the "
                    f"real nanobot/moeka state home ({guarded}): {resolved}. "
                    f"Tests must not resolve paths under the real home -- "
                    f"use tmp_path (or pass an explicit "
                    f"workspace/sessions_root) instead."
                )

    def guarded_path_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(self)
        return original_path_mkdir(self, *args, **kwargs)

    def guarded_os_makedirs(name: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(name)
        return original_os_makedirs(name, *args, **kwargs)

    def guarded_os_mkdir(path: object, *args: object, **kwargs: object) -> object:
        _reject_if_under_real_home(path)
        return original_os_mkdir(path, *args, **kwargs)

    pathlib.Path.mkdir = guarded_path_mkdir  # type: ignore[method-assign]
    os.makedirs = guarded_os_makedirs
    os.mkdir = guarded_os_mkdir


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
    _install_real_home_mkdir_guard()


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

    monkeypatch.setattr(
        "nanobot.session.manager.get_runtime_subdir",
        runtime_subdir,
    )
    monkeypatch.setattr(
        "nanobot.session.manager.get_legacy_sessions_dir",
        lambda: legacy_root,
    )
    # moeka: SqliteSessionStore (nanobot/session/sqlite_store.py) never calls
    # get_runtime_subdir — its default sessions_root is workspace-relative
    # (see default_sessions_root() there), which stays inside whatever tmp_path
    # tree a test already uses, so no redirection is needed for it. It does
    # still read get_legacy_sessions_dir() (informational only, never scanned),
    # imported into its own module namespace, so that one still needs its own
    # patch target.
    monkeypatch.setattr(
        "nanobot.session.sqlite_store.get_legacy_sessions_dir",
        lambda: legacy_root,
    )
    yield


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
