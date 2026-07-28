"""Background-task supervision helpers.

A done-callback factory that drains and logs exceptions from
fire-and-forget tasks so they don't disappear into asyncio's
"exception was never retrieved" path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger


def log_task_exceptions(*, name: str) -> Callable[[asyncio.Task], None]:
    """Return a done-callback that drains and logs swallowed task exceptions.

    Use as::

        task = asyncio.create_task(coro)
        task.add_done_callback(log_task_exceptions(name="my-bg-task"))

    ``CancelledError`` is treated as clean shutdown and not logged. Any
    other exception is routed through ``logger.exception`` so it lands in
    the gateway logs instead of being silently dropped at GC time.
    """

    def _callback(task: asyncio.Task) -> None:
        try:
            if task.cancelled():
                return
            exc = task.exception()
        except Exception:
            # Defensive: a done-callback must never raise (asyncio would
            # log it as a warning and we'd lose the original context).
            return
        if exc is None or isinstance(exc, asyncio.CancelledError):
            return
        logger.opt(exception=exc).error("Background task '{}' failed", name)

    return _callback
