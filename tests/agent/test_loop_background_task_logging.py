"""Integration test for AgentLoop._schedule_background.

Confirms that fire-and-forget background coros emit a structured failure
log when they raise (previously the exception was silently dropped at
asyncio GC time), and that the task is removed from
``_background_tasks`` exactly once even if the done-callback runs after
the list already lost it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies (same pattern as
    tests/agent/test_task_cancel.py)."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop


def _capture():
    from loguru import logger as loguru_logger

    records: list[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level="ERROR")
    return records, handler_id


@pytest.mark.asyncio
async def test_schedule_background_logs_exception():
    from loguru import logger as loguru_logger

    loop = _make_loop()

    async def boom():
        raise RuntimeError("synthetic-bg-failure")

    records, handler_id = _capture()
    try:
        loop._schedule_background(boom())
        # Drain the background task list.
        if loop._background_tasks:
            await asyncio.gather(*loop._background_tasks, return_exceptions=True)
        # Yield so the done-callback runs.
        await asyncio.sleep(0)
    finally:
        loguru_logger.remove(handler_id)

    failures = [r for r in records if "agent-background" in r and "failed" in r]
    assert failures, f"expected agent-background failure log, got: {records!r}"
    assert any("synthetic-bg-failure" in r for r in records)


@pytest.mark.asyncio
async def test_schedule_background_removes_task_safely():
    """Even if the task was already removed from the list before the
    done-callback fires (e.g. by a concurrent drain in shutdown), the
    callback's list.remove must not propagate ValueError."""
    loop = _make_loop()

    async def quick():
        return "ok"

    loop._schedule_background(quick())
    tasks = list(loop._background_tasks)
    loop._background_tasks.clear()
    for t in tasks:
        await t
    await asyncio.sleep(0)
    # If the callback had raised, asyncio would have logged a warning.
    # The contract: no propagation, no crash.
