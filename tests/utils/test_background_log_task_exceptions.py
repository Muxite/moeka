"""Tests for ``log_task_exceptions`` background-task supervisor helper.

This pins the contract: exceptions from fire-and-forget tasks land in
the loguru stream instead of disappearing into asyncio's
``exception was never retrieved`` path, and ``CancelledError`` (clean
shutdown) is silent.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.utils.background import log_task_exceptions


def _capture(level: str = "ERROR"):
    """Return (records, handler_id) for a loguru sink at *level*."""
    from loguru import logger as loguru_logger

    records: list[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level=level)
    return records, handler_id


class TestLogTaskExceptions:
    @pytest.mark.asyncio
    async def test_logs_exception_with_name(self):
        from loguru import logger as loguru_logger

        async def boom():
            raise RuntimeError("kaboom")

        records, handler_id = _capture()
        try:
            task = asyncio.create_task(boom())
            task.add_done_callback(log_task_exceptions(name="under-test"))
            # Wait for task to finish; the done-callback fires synchronously.
            with pytest.raises(RuntimeError):
                await task
            # Yield once so the done-callback runs on the event loop.
            await asyncio.sleep(0)
        finally:
            loguru_logger.remove(handler_id)

        failures = [r for r in records if "Background task 'under-test' failed" in r]
        assert failures, f"expected failure log, got: {records!r}"
        assert any("kaboom" in r for r in records)

    @pytest.mark.asyncio
    async def test_cancelled_error_not_logged(self):
        from loguru import logger as loguru_logger

        async def long_running():
            await asyncio.sleep(60.0)

        records, handler_id = _capture()
        try:
            task = asyncio.create_task(long_running())
            task.add_done_callback(log_task_exceptions(name="quiet-cancel"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        finally:
            loguru_logger.remove(handler_id)

        assert not any("quiet-cancel" in r for r in records), records

    @pytest.mark.asyncio
    async def test_successful_task_not_logged(self):
        from loguru import logger as loguru_logger

        async def ok():
            return 42

        records, handler_id = _capture()
        try:
            task = asyncio.create_task(ok())
            task.add_done_callback(log_task_exceptions(name="ok-task"))
            assert (await task) == 42
            await asyncio.sleep(0)
        finally:
            loguru_logger.remove(handler_id)

        assert not any("ok-task" in r for r in records), records
