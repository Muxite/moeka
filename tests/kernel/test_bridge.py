"""Tests for the sync-over-async bridge (nanobot.kernel.bridge.LoopThread)."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

from nanobot.kernel.bridge import LoopThread

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def bridge():
    lt = LoopThread(name="test-bridge-loop")
    yield lt
    lt.stop(timeout=2.0)


def test_run_from_plain_thread(bridge):
    async def coro():
        await asyncio.sleep(0)
        return 42

    result = bridge.run(coro())
    assert result == 42


def test_run_from_thread_pool_worker(bridge):
    async def coro():
        return "ok"

    results = {}

    def worker():
        results["value"] = bridge.run(coro())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert results["value"] == "ok"


def test_run_from_inside_asyncio_run_in_calling_thread(bridge):
    """The host may itself be inside asyncio.run(...) on its own thread's loop.

    LoopThread.run must still work correctly there because it operates on a
    *different* loop (its own), not the caller's running loop.
    """

    async def coro():
        return "bridged"

    def host_main():
        # host_main runs inside asyncio.run() below, i.e. there IS a running
        # loop on this thread, but it's not the bridge's loop.
        return bridge.run(coro())

    async def host_coro():
        return host_main()

    result = asyncio.run(host_coro())
    assert result == "bridged"


def test_exception_propagates_unchanged(bridge):
    class MyError(ValueError):
        pass

    async def coro():
        raise MyError("boom")

    with pytest.raises(MyError, match="boom"):
        bridge.run(coro())


def test_timeout_raises_timeout_error_and_cancels_coroutine(bridge):
    cancelled = threading.Event()

    async def coro():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        bridge.run(coro(), timeout=0.05)

    assert cancelled.wait(timeout=2.0), "coroutine never observed CancelledError"


def test_reentrant_call_from_loop_thread_raises(bridge):
    bridge.start()

    async def inner():
        return 1

    async def coro():
        # Calling bridge.run() from inside the bridge's own loop thread must
        # raise instead of deadlocking.
        pending = inner()
        with pytest.raises(RuntimeError, match="own loop thread"):
            bridge.run(pending)
        pending.close()  # never scheduled; avoid an "unawaited coroutine" warning
        return "checked"

    assert bridge.run(coro()) == "checked"


def test_contextvar_visible_inside_coroutine(bridge):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("bridge_test_var")
    var.set("from-caller")

    async def coro():
        return var.get()

    assert bridge.run(coro()) == "from-caller"


def test_iterate_full_consumption(bridge):
    async def agen():
        for i in range(5):
            yield i

    items = list(bridge.iterate(agen()))
    assert items == [0, 1, 2, 3, 4]


def test_iterate_early_break_closes_agen(bridge):
    closed = {"flag": False}

    async def agen():
        try:
            for i in range(1000):
                yield i
        finally:
            closed["flag"] = True

    it = bridge.iterate(agen())
    collected = []
    for item in it:
        collected.append(item)
        if item == 2:
            it.close()  # explicit: breaking alone only drops a ref, GC timing
            break        # is unspecified, so the consumer closes deterministically

    assert collected == [0, 1, 2]
    assert closed["flag"] is True


def test_drain_lets_short_task_finish_then_stops():
    lt = LoopThread(name="drain-short")
    done = threading.Event()

    async def short_task():
        await asyncio.sleep(0.05)
        done.set()

    lt.submit(short_task())
    time.sleep(0.01)  # let it get scheduled before stop() drains
    lt.stop(timeout=2.0)
    assert done.is_set()


def test_stop_cancels_long_task_after_drain_timeout():
    lt = LoopThread(name="drain-long")
    cancelled = threading.Event()

    async def long_task():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    lt.submit(long_task())
    time.sleep(0.01)
    lt.stop(timeout=0.1)
    assert cancelled.wait(timeout=2.0)


def test_run_after_stop_raises():
    lt = LoopThread(name="stop-then-run")
    lt.start()
    lt.stop(timeout=2.0)

    async def coro():
        return 1

    pending = coro()
    with pytest.raises(RuntimeError, match="bridge stopped"):
        lt.run(pending)
    pending.close()  # never scheduled; avoid an "unawaited coroutine" warning


def test_fifty_threads_submit_concurrently(bridge):
    results = [None] * 50
    errors = []

    def worker(i):
        async def coro():
            await asyncio.sleep(0)
            return i * i

        try:
            results[i] = bridge.run(coro())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    assert results == [i * i for i in range(50)]
