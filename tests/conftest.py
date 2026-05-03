"""Session-level test fixtures shared across all test modules."""

from __future__ import annotations

import glob
import os

import pytest


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
