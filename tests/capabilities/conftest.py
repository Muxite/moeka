"""Shared setup for the capability tests (spec 011)."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.bench`` timing tests unless the run selects them (``-m bench``)."""
    if "bench" in (config.getoption("markexpr", default="") or ""):
        return
    skip = pytest.mark.skip(reason="timing benchmark: run with -m bench")
    for item in items:
        if item.get_closest_marker("bench") is not None:
            item.add_marker(skip)
