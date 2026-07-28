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
