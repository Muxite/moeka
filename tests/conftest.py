"""Session-level test fixtures shared across all test modules."""

from __future__ import annotations

import glob
import os

import pytest

# Embedding tests must not fight the live moeka service (or each other) for
# VRAM: a handful of per-test SentenceTransformer loads can OOM the GPU and
# VecStore then degrades to empty results, failing assertions spuriously.
# The test models are tiny — CPU is fast and deterministic. Set before any
# torch import; export CUDA_VISIBLE_DEVICES yourself to override.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


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
    """Fail loudly if any test opens a SessionManager on the live ~/.nanobot.

    SessionManager construction now has side effects (sessions.db creation,
    one-time jsonl import), so a test leaking onto the real workspace can
    move the user's live session data. Hermetic tests must use tmp_path.
    """
    from pathlib import Path

    from nanobot.session import manager as _manager

    live = (Path.home() / ".nanobot").resolve()
    orig_init = _manager.SessionManager.__init__

    def guarded_init(self, workspace):
        ws = Path(workspace).expanduser().resolve()
        if ws == live:
            raise AssertionError(
                "TEST LEAK: SessionManager constructed on the live ~/.nanobot "
                "workspace — use tmp_path instead"
            )
        orig_init(self, workspace)

    _manager.SessionManager.__init__ = guarded_init
    yield
    _manager.SessionManager.__init__ = orig_init


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
