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


@pytest.fixture
def caplog_warnings():
    """Loguru messages at WARNING and above emitted during the test."""
    from loguru import logger

    records: list[str] = []
    handler = logger.add(lambda message: records.append(str(message.record["message"])),
                         level="WARNING", format="{message}")
    yield records
    try:
        logger.remove(handler)
    except ValueError:
        pass
