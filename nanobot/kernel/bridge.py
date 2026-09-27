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
    deadlocking. All state transitions (idle -> running -> stopping ->
    stopped) happen under one lock, so ``start``/``run``/``submit`` and
    ``stop`` are safe to call concurrently from any number of threads; the
    loop never restarts once stopped.
    """

    def __init__(self, name: str = "moeka-kernel-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "idle"  # idle -> running -> stopping -> stopped
        self._stopped_event = threading.Event()
        # future -> the raw coroutine it wraps, until a task takes ownership
        # of it (then the entry is nulled to None). Lets a leftover callback
        # that never got to run be settled *and* have its coroutine closed,
        # instead of leaking a "coroutine was never awaited" warning.
        self._pending: dict[Future[Any], Any] = {}

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Start the loop thread if it isn't already running. Idempotent.

        Raises ``RuntimeError`` if the bridge has already been stopped
        (or is in the process of stopping) — it never restarts.
        """
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        # Caller holds self._lock.
        if self._state == "running":
            return
        if self._state in ("stopping", "stopped"):
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
                self._fail_pending_futures()

        thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread = thread
        thread.start()
        ready.wait()
        self._state = "running"

    def _check_not_on_loop_thread(self, method: str) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                f"LoopThread.{method}() called from its own loop thread would deadlock; "
                "await the async method directly instead"
            )

    def _fail_pending_futures(self) -> None:
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
        for fut, coro in pending.items():
            if not fut.done():
                try:
                    fut.set_exception(RuntimeError("bridge stopped"))
                except Exception:  # noqa: BLE001 - fut may settle concurrently
                    pass
            if coro is not None:
                try:
                    coro.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    def _settle(
        self,
        fut: Future[Any],
        *,
        exc: BaseException | None = None,
        result: Any = None,
        cancelled: bool = False,
    ) -> None:
        with self._lock:
            self._pending.pop(fut, None)
        if fut.done():
            return
        try:
            if cancelled:
                fut.cancel()
            elif exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(result)
        except Exception:  # noqa: BLE001 - fut may have settled concurrently
            pass

    # -- submission ----------------------------------------------------

    def _schedule(self, coro: Any, method_name: str) -> tuple[Future[Any], dict[str, Any]]:
        """Schedule ``coro`` on the loop; return its future and a task handle.

        The handle is a plain ``dict`` with a ``"task"`` key, filled in once
        the task is actually created on the loop (or left ``None`` if the
        bridge rejected the call before that point). Both ``run`` and
        ``submit`` build on this so the accept/reject/cancel logic lives in
        one place.

        The state check, the bookkeeping that lets a stopped bridge close
        this exact coroutine later, and the actual scheduling onto the loop
        all happen under one lock hold — so a ``stop()`` that runs
        concurrently either happens strictly before this call (and the call
        is rejected up front) or strictly after it (and the resulting task
        is guaranteed to be visible to ``stop()``'s drain). There is no
        window where the call is accepted against a loop that's already
        gone.
        """
        try:
            self._check_not_on_loop_thread(method_name)
        except RuntimeError:
            coro.close()
            raise

        ctx = contextvars.copy_context()
        fut: Future[Any] = Future()
        handle: dict[str, Any] = {"task": None}

        def _on_fut_settled(f: Future[Any]) -> None:
            # Lets fut.cancel() (submit's caller cancelling, or run()'s own
            # timeout handling) reach the underlying task, matching
            # asyncio.run_coroutine_threadsafe's cancel semantics.
            if not f.cancelled():
                return
            task = handle.get("task")
            if task is not None and not task.done():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # loop already closed

        def _on_loop() -> None:
            with self._lock:
                accept = self._state == "running"
            if not accept:
                coro.close()
                self._settle(fut, exc=RuntimeError("bridge stopped"))
                return
            if fut.done():
                # Cancelled (or otherwise settled) before we got to start it.
                coro.close()
                with self._lock:
                    self._pending.pop(fut, None)
                return
            try:
                task = loop.create_task(coro, context=ctx)
            except Exception as exc:  # noqa: BLE001 - surface to the caller's future
                self._settle(fut, exc=exc)
                return
            handle["task"] = task
            with self._lock:
                # The task now owns the coroutine; nothing left to close by
                # hand even if the bridge stops before the task finishes.
                self._pending[fut] = None
            if fut.cancelled():
                # Cancelled between the fut.done() check above and task
                # creation (e.g. create_task() itself yielding to another
                # thread) — stop it before its body ever runs.
                task.cancel()

            def _on_task_done(t: asyncio.Task[Any]) -> None:
                if fut.done():
                    return
                if t.cancelled():
                    self._settle(fut, cancelled=True)
                    return
                exc = t.exception()
                if exc is not None:
                    self._settle(fut, exc=exc)
                else:
                    self._settle(fut, result=t.result())

            task.add_done_callback(_on_task_done)

        with self._lock:
            if self._state in ("stopping", "stopped"):
                coro.close()
                fut.set_exception(RuntimeError("bridge stopped"))
                return fut, {}
            if self._state == "idle":
                self._start_locked()
            loop = self._loop
            assert loop is not None
            self._pending[fut] = coro
            try:
                loop.call_soon_threadsafe(_on_loop)
            except RuntimeError:
                # Only reachable if the loop closed despite us holding the
                # lock stop() also needs to transition state — defensive.
                self._pending.pop(fut, None)
                coro.close()
                fut.set_exception(RuntimeError("bridge stopped"))
                return fut, {}

        fut.add_done_callback(_on_fut_settled)
        return fut, handle

    def submit(self, coro: Any) -> Future[Any]:
        """Schedule ``coro`` on the loop; return a ``concurrent.futures.Future``.

        Cancelling the returned future (``fut.cancel()``) cancels the
        underlying task too, if it has been created by then.
        """
        fut, _handle = self._schedule(coro, "submit")
        return fut

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Run ``coro`` on the loop and block until it finishes.

        Re-raises the coroutine's own exception unchanged. Rejected calls
        (bridge stopping/stopped) always raise ``RuntimeError("bridge
        stopped")``. On timeout, cancels the underlying task (or, if it
        hasn't been created on the loop yet, cancels it the moment it is —
        the cancel request is itself delivered on the loop, after any
        already-queued task creation), waits (bounded) for the cancellation
        to actually land, then raises builtin ``TimeoutError``.
        """
        fut, handle = self._schedule(coro, "run")
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            loop = self._loop
            if loop is not None:

                def _cancel_on_loop() -> None:
                    task = handle.get("task")
                    if task is not None and not task.done():
                        task.cancel()

                try:
                    loop.call_soon_threadsafe(_cancel_on_loop)
                except RuntimeError:
                    pass  # loop already closed
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
        """Stop the loop thread. Idempotent and safe to call concurrently.

        Lets pending tasks finish for up to ``timeout`` seconds (drain,
        re-checked in a loop so tasks created mid-drain are also waited
        on), then cancels whatever is left, runs ``shutdown_asyncgens``,
        and joins the thread. After this, ``run``/``submit`` always raise
        ``RuntimeError("bridge stopped")``. Concurrent callers all block
        until the one doing the actual work finishes; none of them raise.
        Calling this from the bridge's own loop thread would deadlock (the
        thread would be joining itself), so it raises immediately instead,
        matching ``run``/``submit``/``iterate``.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "LoopThread.stop() called from its own loop thread would deadlock; "
                "call it from another thread instead"
            )

        with self._lock:
            if self._state == "stopped":
                return
            if self._state == "idle":
                # Never started: nothing to drain or join.
                self._state = "stopped"
                self._stopped_event.set()
                return
            if self._state == "stopping":
                owner = False
            else:  # "running"
                self._state = "stopping"
                owner = True

        if not owner:
            # Bounded: the owner's try/finally below always resolves this,
            # but a caller here should never block forever regardless.
            self._stopped_event.wait(timeout=timeout + 15.0)
            return

        loop = self._loop
        thread = self._thread
        assert loop is not None and thread is not None

        try:
            async def _drain_and_shutdown() -> None:
                running_loop = asyncio.get_running_loop()
                deadline = running_loop.time() + timeout
                while True:
                    current = asyncio.current_task()
                    pending = [
                        t for t in asyncio.all_tasks() if t is not current and not t.done()
                    ]
                    if not pending:
                        break
                    remaining = deadline - running_loop.time()
                    if remaining <= 0:
                        break
                    await asyncio.wait(pending, timeout=remaining)
                    # Loop again: draining may itself have let new tasks start.
                current = asyncio.current_task()
                stragglers = [
                    t for t in asyncio.all_tasks() if t is not current and not t.done()
                ]
                for t in stragglers:
                    t.cancel()
                if stragglers:
                    await asyncio.gather(*stragglers, return_exceptions=True)
                await running_loop.shutdown_asyncgens()

            drain_done: Future[None] = Future()

            def _start_drain() -> None:
                task = loop.create_task(_drain_and_shutdown())

                def _done(t: asyncio.Task[None]) -> None:
                    if t.cancelled():
                        drain_done.set_result(None)
                        return
                    exc = t.exception()
                    if exc is not None:
                        drain_done.set_exception(exc)
                    else:
                        drain_done.set_result(None)

                task.add_done_callback(_done)

            try:
                loop.call_soon_threadsafe(_start_drain)
            except RuntimeError as exc:
                drain_done.set_exception(exc)

            try:
                drain_done.result(timeout=timeout + 5.0)
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise into caller
                logger.warning("LoopThread.stop: drain/shutdown failed: {!r}", exc)

            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # loop already stopped/closed some other way
            thread.join(timeout=timeout + 5.0)
            # _run's finally already closed the loop and failed (and closed
            # the coroutines of) any leftover pending futures once
            # run_forever() returned above.
        finally:
            with self._lock:
                self._state = "stopped"
            self._stopped_event.set()
