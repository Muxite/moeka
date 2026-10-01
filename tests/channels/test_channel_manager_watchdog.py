"""Tests for the moeka dispatcher watchdog (``_dispatch_with_watchdog``).

CLAUDE.md calls out the watchdog as a moeka-specific deviation that
auto-restarts the outbound dispatcher when it crashes. These tests pin
the contract: a single uncaught exception in ``_dispatch_outbound`` does
not propagate, the restart counter increments, the dispatcher resumes,
and ``CancelledError`` shuts down cleanly without inflating the counter.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


@pytest.fixture(autouse=True)
def _fast_restart(monkeypatch):
    """Shrink the watchdog restart delay so tests don't pay wall-clock cost.

    Overrides the class attribute (not the method body) so the test runs
    the *real* production watchdog code path.
    """
    monkeypatch.setattr(ChannelManager, "_DISPATCH_RESTART_DELAY_S", 0.01)


@pytest.fixture
def manager() -> ChannelManager:
    return ChannelManager(Config(), MessageBus())


class TestDispatcherWatchdog:
    @pytest.mark.asyncio
    async def test_restart_count_starts_at_zero(self, manager: ChannelManager):
        assert manager._dispatch_restart_count == 0

    @pytest.mark.asyncio
    async def test_watchdog_restarts_on_unexpected_exception(
        self, manager: ChannelManager, monkeypatch
    ):
        """After a single crash, the watchdog should restart and let the
        replacement run finish cleanly without re-raising."""
        calls = {"n": 0}
        done = asyncio.Event()

        async def fake_dispatch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            done.set()

        monkeypatch.setattr(manager, "_dispatch_outbound", fake_dispatch)

        task = asyncio.create_task(manager._dispatch_with_watchdog())
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=1.0)

        assert calls["n"] == 2
        assert manager._dispatch_restart_count == 1

    @pytest.mark.asyncio
    async def test_watchdog_logs_crash_with_count(
        self, manager: ChannelManager, monkeypatch
    ):
        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(
            lambda m: records.append(str(m)), level="ERROR"
        )
        try:
            done = asyncio.Event()
            calls = {"n": 0}

            async def fake_dispatch():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("synthetic crash")
                done.set()

            monkeypatch.setattr(manager, "_dispatch_outbound", fake_dispatch)

            task = asyncio.create_task(manager._dispatch_with_watchdog())
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await asyncio.wait_for(task, timeout=1.0)
        finally:
            loguru_logger.remove(handler_id)

        crashes = [r for r in records if "dispatcher crashed" in r]
        assert len(crashes) == 1, f"expected one crash log, got: {crashes!r}"
        # The log line must carry the restart counter so journalctl viewers
        # can tell a one-off crash from a crash loop at a glance.
        assert re.search(r"count=1\b", crashes[0]), crashes[0]
        assert "synthetic crash" in crashes[0]

    @pytest.mark.asyncio
    async def test_cancelled_error_does_not_increment_counter(
        self, manager: ChannelManager, monkeypatch
    ):
        """Clean shutdown via task.cancel() must not look like a crash."""
        started = asyncio.Event()

        async def fake_dispatch():
            started.set()
            # Block until cancelled.
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                raise

        monkeypatch.setattr(manager, "_dispatch_outbound", fake_dispatch)

        task = asyncio.create_task(manager._dispatch_with_watchdog())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager._dispatch_restart_count == 0

    @pytest.mark.asyncio
    async def test_restart_count_escalates_to_critical_above_threshold(
        self, manager: ChannelManager, monkeypatch
    ):
        from loguru import logger as loguru_logger

        records: list[tuple[str, str]] = []
        handler_id = loguru_logger.add(
            lambda m: records.append((m.record["level"].name, str(m))),
            level="ERROR",
        )
        try:
            done = asyncio.Event()
            crashes_before_success = ChannelManager._DISPATCH_RESTART_WARN
            calls = {"n": 0}

            async def fake_dispatch():
                calls["n"] += 1
                if calls["n"] <= crashes_before_success:
                    raise RuntimeError(f"crash-{calls['n']}")
                done.set()

            monkeypatch.setattr(manager, "_dispatch_outbound", fake_dispatch)

            task = asyncio.create_task(manager._dispatch_with_watchdog())
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await asyncio.wait_for(task, timeout=1.0)
        finally:
            loguru_logger.remove(handler_id)

        levels = [lvl for lvl, text in records if "dispatcher crashed" in text]
        # First N-1 logs are ERROR; the threshold crossing and beyond are CRITICAL.
        assert "CRITICAL" in levels, f"expected CRITICAL after threshold, got: {levels}"
        assert levels.count("ERROR") == ChannelManager._DISPATCH_RESTART_WARN - 1, (
            f"expected {ChannelManager._DISPATCH_RESTART_WARN - 1} ERRORs, got: {levels}"
        )
