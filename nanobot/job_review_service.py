"""Downtime review (spec 015 phase 4): look at the job record when idle and ask the owner.

Host service. When no job is active it looks for work that keeps recurring and for the same
failure repeating, and asks the owner once per pattern, inside quiet hours and a daily cap
(``UnsolicitedDeliveryLimit``). It never builds anything by itself: a new skill needs the
evidence rule in ``nanobot.kernel.job_review`` or the owner's ``/approve-skill``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.kernel.job_review import MIN_REUSE, find_failure_clusters, find_repeats
from nanobot.kernel.jobs import JobStore

WINDOW = timedelta(days=14)


def build_digest(store: JobStore) -> tuple[str, list[tuple[str, str]]]:
    """(message, [(question key, kind)]) for patterns not asked about yet; ("", []) if none."""
    since = (datetime.now(timezone.utc) - WINDOW).isoformat(timespec="milliseconds")
    lines: list[str] = []
    keys: list[tuple[str, str]] = []
    for c in find_repeats(store, since=since, min_count=2):
        key = f"repeat:{c.key}"
        if store.was_asked(key):
            continue
        extra = (" That is enough repeats that I may suggest a standing skill."
                 if c.count >= MIN_REUSE else "")
        lines.append(f"- You have asked for something like \"{c.sample}\" {c.count} times.{extra} "
                     "Is this a task I will keep doing, and is it worth a reusable skill?")
        keys.append((key, "repeat"))
    for c in find_failure_clusters(store, since=since, min_count=3):
        key = f"failure:{c.key}"
        if store.was_asked(key):
            continue
        lines.append(f"- {c.count} jobs stopped with the same cause ({c.key}); the latest was "
                     f"\"{c.sample}\". Should I change how I handle this, or is it expected?")
        keys.append((key, "failure"))
    if not lines:
        return "", []
    head = "Quiet-time review of my own job record. A few questions, no action taken:\n"
    tail = ("\nIf a recurring task deserves a skill, say /approve-skill <note>. Otherwise just "
            "tell me and I will stop asking about it.")
    return head + "\n".join(lines[:5]) + tail, keys[:5]


class JobReviewer:
    def __init__(
        self, store: JobStore, deliver: Callable[..., Awaitable[None]],
        target: Callable[[], tuple[str, str]], limiter, timezone_name: str = "UTC",
        *, interval_s: float = 6 * 3600,
    ) -> None:
        self.store, self._deliver, self._target = store, deliver, target
        self._limiter, self._tz, self._interval = limiter, timezone_name, interval_s
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="moeka-job-review")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> bool:
        """One review pass. Returns True when a question was sent."""
        if self.store.active():
            return False  # only in downtime
        text, keys = build_digest(self.store)
        if not keys:
            return False
        if not self._limiter.reserve(timezone=self._tz, max_per_day=1):
            return False  # quiet hours or already asked today; try again later
        channel, chat_id = self._target()
        await self._deliver(OutboundMessage(channel=channel, chat_id=chat_id, content=text),
                            record=True)
        for key, kind in keys:
            self.store.mark_asked(key, kind)
        return True

    async def _run(self) -> None:
        await asyncio.sleep(300)  # let the gateway settle first
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job review pass failed")
            await asyncio.sleep(self._interval)
