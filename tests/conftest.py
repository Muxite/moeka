"""Session-level test fixtures shared across all test modules."""

from __future__ import annotations

import glob
import os

import pytest

# Prime the lazy forward-ref rebuild at conftest import time (before test
# modules are collected). moeka keeps Config/ToolsConfig forward refs resolved
# lazily to survive circular-import order at runtime; several upstream tests
# import tool-config classes (e.g. WebSearchConfig, ImageGenerationToolConfig)
# directly from nanobot.config.schema at module top-level, which only works
# once the rebuild has run. Triggering it here makes that import order safe for
# the whole test session without disturbing the runtime lazy path.
try:
    from nanobot.config.schema import _resolve_tool_config_refs

    _resolve_tool_config_refs()
except Exception:
    pass

# Embedding tests must not fight the live moeka service (or each other) for
# VRAM: a handful of per-test SentenceTransformer loads can OOM the GPU and
# VecStore then degrades to empty results, failing assertions spuriously.
# The test models are tiny — CPU is fast and deterministic. Set before any
# torch import; export CUDA_VISIBLE_DEVICES yourself to override.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def pytest_collection_finish(session: pytest.Session) -> None:
    """Fail the run if far fewer tests were collected than expected.

    Roughly 380 tests — about 11% of the suite — sit behind module-level
    ``importorskip`` / ``pytest.skip(allow_module_level=True)`` guards for
    optional channel extras (telegram, matrix, discord, feishu, msteams, wecom,
    slack, dingtalk, qq, and the sqlite_vec / sentence_transformers pair).

    That is the right behaviour for a contributor without those extras. It is a
    silent disaster in CI: if ``uv sync --all-extras`` fails to resolve one of
    the low-maintenance channel packages on a new Python, every test in that
    module vanishes and the build goes **green** with hundreds fewer tests. A
    dependency failure becomes a false pass, which is worse than a red build.

    Opt-in by design (see .agent/design.md, "explicit over magical") so that
    running a subset locally — ``pytest tests/cron`` — never trips it. CI sets
    ``NANOBOT_MIN_TESTS``; nothing else does.
    """
    floor = os.environ.get("NANOBOT_MIN_TESTS")
    if not floor:
        return
    collected = len(session.items)
    if collected < int(floor):
        raise pytest.UsageError(
            f"Collected only {collected} tests, expected at least {floor}. "
            "This usually means an optional dependency failed to install and a "
            "module-level skip silently dropped its whole file — check the "
            "install step and the -ra skip summary rather than lowering this "
            "floor."
        )


@pytest.fixture(autouse=True)
def _restore_os_environ():
    """Snapshot/restore ``os.environ`` around every test.

    Originally added for a specific leak: ``OpenAICompatProvider._setup_env``
    wrote the resolved api key into ``os.environ`` and, for *gateway* specs like
    OpenRouter, **overwrote** an existing value. A test constructing a provider
    with a placeholder key such as ``"sk-test-key"`` therefore replaced the real
    ``OPENROUTER_API_KEY`` that ``tests/core/test_integration_real.py`` loads
    from ``keys.env`` at collection time — so the live tests passed alone and
    failed in a full run.

    **That leak is fixed at the source**: ``_setup_env`` has been removed and
    ``tests/providers/test_no_env_mutation.py`` guards its return. This fixture
    is kept anyway, as general isolation — several tests still write env vars
    directly, and a per-test restore is what keeps the suite order-independent
    regardless of which future code path leaks next.

    Caveat worth knowing: this is function-scoped, so it cannot undo writes that
    happen at *collection* time (``test_integration_real.py`` calls
    ``_load_keys_env()`` at module import, before any fixture runs).
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reenable_nanobot_logging():
    """Undo loguru ``logger.disable("nanobot")`` leaking across tests.

    CLI command paths (nanobot/cli/commands.py) disable the "nanobot"
    namespace when run without --verbose/--logs. loguru's disable is global
    process state, so once a CLI test exercises that path, every later test
    asserting on captured loguru records sees nothing. Re-enable before each
    test so log-capture tests are order-independent.
    """
    from loguru import logger

    logger.enable("nanobot")
    yield


@pytest.fixture(autouse=True, scope="session")
def _guard_live_workspace():
    """Fail loudly if any test opens a session store on the live ~/.nanobot.

    SessionManager/SqliteSessionStore/JsonlSessionStore construction has
    side effects (sessions.db creation, one-time jsonl import, and for
    SqliteSessionStore, ADR-0001's out-of-workspace *migration* of an
    existing sessions.db) -- a test leaking onto the real workspace can
    move the user's live session data. Hermetic tests must use tmp_path.

    Belt-and-suspenders with the root conftest.py's $HOME redirect + mkdir/
    rename/move/sqlite3.connect guards (which are the authoritative,
    always-on mechanism): this fixture guards the specific constructors
    directly, in case some future refactor adds another one that doesn't go
    through any of those operations at all. See
    .agent/upstream-sync-notes.md's 2026-09-17 incident writeup, and its
    review-fixes follow-up, for why both layers exist.
    """
    from pathlib import Path

    from nanobot.session import manager as _manager
    from nanobot.session import sqlite_store as _sqlite_store
    from tests._home_guard import REAL_NANOBOT_HOME

    # Computed from the real $HOME captured before pytest's root conftest.py
    # redirects it (see tests/_home_guard.py's docstring) -- NOT
    # Path.home() here, which by the time this fixture runs has already
    # been redirected to an ephemeral test-session directory. Recomputing
    # from Path.home() at fixture time was exactly the bug the 2026-09-17
    # review caught: this guard could never fire because it was comparing
    # against the fake home, not the real one.
    live = REAL_NANOBOT_HOME
    orig_session_manager_init = _manager.SessionManager.__init__
    orig_jsonl_store_init = _manager.JsonlSessionStore.__init__
    orig_sqlite_store_init = _sqlite_store.SqliteSessionStore.__init__

    def _check(workspace: object) -> None:
        try:
            ws = Path(workspace).expanduser().resolve()  # type: ignore[arg-type]
        except (TypeError, ValueError, OSError):
            return
        if ws == live:
            raise AssertionError(
                "TEST LEAK: a session store was constructed on the live "
                "~/.nanobot workspace — use tmp_path instead"
            )

    def guarded_session_manager_init(self, workspace, **kwargs):
        _check(workspace)
        orig_session_manager_init(self, workspace, **kwargs)

    def guarded_jsonl_store_init(self, workspace, **kwargs):
        _check(workspace)
        orig_jsonl_store_init(self, workspace, **kwargs)

    def guarded_sqlite_store_init(self, workspace, **kwargs):
        _check(workspace)
        orig_sqlite_store_init(self, workspace, **kwargs)

    _manager.SessionManager.__init__ = guarded_session_manager_init
    _manager.JsonlSessionStore.__init__ = guarded_jsonl_store_init
    _sqlite_store.SqliteSessionStore.__init__ = guarded_sqlite_store_init
    yield
    _manager.SessionManager.__init__ = orig_session_manager_init
    _manager.JsonlSessionStore.__init__ = orig_jsonl_store_init
    _sqlite_store.SqliteSessionStore.__init__ = orig_sqlite_store_init


@pytest.fixture(autouse=True, scope="session")
def _cleanup_mock_path_artifacts():
    """Delete <MagicMock …> files that some tests leave in the project root.

    These are created when a test mocks a Path object and the mock's
    __truediv__ result is later coerced to a string (e.g. passed to open()).
    """
    yield
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for path in glob.glob(os.path.join(root, "<MagicMock*")):
        try:
            os.unlink(path)
        except OSError:
            pass
