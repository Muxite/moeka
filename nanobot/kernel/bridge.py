"""Sync-over-async bridge: one loop thread that makes async twins safe from
any calling thread, including from inside a host's own running event loop
(stdlib + loguru only).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class LoopThread:
    """A daemon thread running its own asyncio event loop forever.

    Lets any other thread submit coroutines to it and block for the result
    (:meth:`run`), fire-and-forget (:meth:`submit`), or pull an async
    iterator through it item by item (:meth:`iterate`). Calling from the
    loop thread's own running loop raises ``RuntimeError`` instead of
    deadlocking.
    """

    def __init__(self, name: str = "moeka-kernel-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._started = threading.Event()
        self._stopped = False

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Start the loop thread if it isn't already running. Idempotent."""
        if self._started.is_set():
            return
        with self._start_lock:
            if self._started.is_set():
                return
            if self._stopped:
                raise RuntimeError("bridge stopped")
            ready = threading.Event()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(target=_run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()
            ready.wait()
            self._started.set()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        if self._stopped:
            raise RuntimeError("bridge stopped")
        if not self._started.is_set():
            self.start()
        assert self._loop is not None
        return self._loop

    def _check_not_on_loop_thread(self, method: str) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                f"LoopThread.{method}() called from its own loop thread would deadlock; "
                "await the async method directly instead"
            )

    # -- submission ----------------------------------------------------

    def submit(self, coro: Any) -> Future[Any]:
        """Schedule ``coro`` on the loop; return a ``concurrent.futures.Future``."""
        loop = self._ensure_started()
        self._check_not_on_loop_thread("submit")
        ctx = contextvars.copy_context()
        fut: Future[Any] = Future()

        def _start() -> None:
            try:
                task = loop.create_task(coro, context=ctx)
            except Exception as exc:  # noqa: BLE001 - surface failure to the caller's future
                fut.set_exception(exc)
                return

            def _done(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    fut.cancel()
                    return
                exc = t.exception()
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(t.result())

            task.add_done_callback(_done)

        loop.call_soon_threadsafe(_start)
        return fut

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Run ``coro`` on the loop and block until it finishes.

        Re-raises the coroutine's own exception unchanged. On timeout,
        cancels the underlying task, waits (bounded) for the cancellation
        to actually land, then raises ``TimeoutError``.
        """
        loop = self._ensure_started()
        self._check_not_on_loop_thread("run")
        ctx = contextvars.copy_context()
        task_holder: dict[str, asyncio.Task[Any]] = {}
        fut: Future[Any] = Future()

        def _start() -> None:
            try:
                task = loop.create_task(coro, context=ctx)
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
                return
            task_holder["task"] = task

            def _done(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    fut.cancel()
                    return
                exc = t.exception()
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(t.result())

            task.add_done_callback(_done)

        loop.call_soon_threadsafe(_start)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            task = task_holder.get("task")
            if task is not None:

                def _cancel() -> None:
                    task.cancel()

                loop.call_soon_threadsafe(_cancel)
                # Bound the wait for cancellation to actually land; don't hang
                # forever if the coroutine swallows CancelledError.
                try:
                    fut.result(timeout=5.0)
                except Exception:  # noqa: BLE001 - we only care that it settled
                    pass
            raise TimeoutError(f"LoopThread.run timed out after {timeout}s") from None

    def iterate(
        self, agen: AsyncIterator[T], timeout_per_item: float | None = None
    ) -> Iterator[T]:
        """A sync iterator over an async iterator, pulled through the loop.

        If the consumer stops early (break, generator close, or an
        exception raised into this generator), ``aclose()`` is called on
        the async iterator on the loop.
        """
        self._ensure_started()
        self._check_not_on_loop_thread("iterate")
        closed = False

        def _aclose() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            aclose = getattr(agen, "aclose", None)
            if aclose is None:
                return
            try:
                self.run(aclose(), timeout=timeout_per_item)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.debug("LoopThread.iterate: aclose failed: {!r}", exc)

        try:
            while True:
                try:
                    item = self.run(agen.__anext__(), timeout=timeout_per_item)
                except StopAsyncIteration:
                    closed = True
                    return
                yield item
        finally:
            if not closed:
                _aclose()

    # -- shutdown --------------------------------------------------------

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop thread. Idempotent.

        Lets pending tasks finish for up to ``timeout`` seconds (drain),
        then cancels whatever is left, runs ``shutdown_asyncgens``, and
        joins the thread. After this, ``run``/``submit`` raise
        ``RuntimeError``.
        """
        if self._stopped:
            return
        if not self._started.is_set():
            # Never started: nothing to drain or join.
            self._stopped = True
            return

        loop = self._loop
        assert loop is not None

        drain_done: Future[None] = Future()

        async def _drain_and_shutdown() -> None:
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=timeout)
            still_pending = [t for t in pending if not t.done()]
            for t in still_pending:
                t.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
            await loop.shutdown_asyncgens()

        def _start_drain() -> None:
            task = loop.create_task(_drain_and_shutdown())

            def _done(t: asyncio.Task[None]) -> None:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None:
                    drain_done.set_exception(exc)
                else:
                    drain_done.set_result(None)

            task.add_done_callback(_done)

        loop.call_soon_threadsafe(_start_drain)
        try:
            drain_done.result(timeout=timeout + 5.0)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise into caller
            logger.warning("LoopThread.stop: drain/shutdown failed: {!r}", exc)

        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout + 5.0)
        self._stopped = True
