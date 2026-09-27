"""``Kernel``: the host-facing engine object built from an :class:`Environment`.

This is the skeleton: it owns the environment, the trace sink and a private
loop thread (the sync-twin bridge), and has an idempotent close in both sync
and async form. The LLM layer, agents, sessions and memory attach here.
"""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType

from nanobot.kernel.bridge import LoopThread
from nanobot.kernel.hostenv import Environment
from nanobot.kernel.trace import TraceSink


class Kernel:
    """One kernel per host environment. Use as a (sync or async) context manager,
    or call :meth:`close` / :meth:`aclose` when done; both are idempotent."""

    def __init__(self, env: Environment) -> None:
        if not isinstance(env, Environment):
            raise TypeError(
                f"Kernel(env) needs a moeka Environment, got {type(env).__name__}; "
                "build one with Environment.for_host(...) or Environment.from_config(...)"
            )
        self._env = env
        self._closed = False
        self._close_lock = threading.Lock()
        # Internal: the loop that ``*_sync`` twins run on. Starts lazily on first use.
        self._bridge = LoopThread(name="moeka-kernel-loop")

    @property
    def env(self) -> Environment:
        return self._env

    @property
    def trace(self) -> TraceSink:
        return self._env.trace

    @property
    def closed(self) -> bool:
        return self._closed

    # -- lifecycle -------------------------------------------------------

    def _reject_loop_thread(self, method: str) -> None:
        if threading.current_thread() is self._bridge._thread:
            raise RuntimeError(
                f"Kernel.{method}() called from the kernel's own loop thread would join "
                "itself; close the kernel from host code instead"
            )

    def close(self) -> None:
        """Release kernel resources and stop the loop thread. Idempotent.

        Raises ``RuntimeError`` when called from the kernel's own loop thread
        (it would join itself), leaving the kernel open. Concurrent callers
        block until the first close has finished, so a return always means
        the loop thread is gone.
        """
        self._reject_loop_thread("close")
        with self._close_lock:
            if self._closed:
                return
            self._bridge.stop()
            # Only after stop() returned: a failed stop leaves the kernel open
            # so a later close() can retry.
            self._closed = True

    async def aclose(self) -> None:
        """Async :meth:`close`: stops the loop thread off the caller's event loop."""
        self._reject_loop_thread("aclose")
        # ``_closed`` is set only once the thread is stopped, so this fast path
        # never returns while another thread's close is still in progress.
        if self._closed:
            return
        await asyncio.to_thread(self.close)

    def __enter__(self) -> Kernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Kernel:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"Kernel({self._env!r}, {state})"


__all__ = ["Kernel"]
