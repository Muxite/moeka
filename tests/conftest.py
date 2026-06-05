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
