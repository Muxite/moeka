"""Owner notifier: tells the owner, on its own, when a job needs them or finishes (spec 015).

Reads the durable ``job_notifications`` outbox in the job store and sends each entry to the
chat the job came from, through the gateway's ``_deliver_to_channel`` so the owner's reply
has context. Host code: the kernel records, the host delivers.

Rules, kept in :func:`decide` so they are testable without a gateway:
- A chat turn's job (role ``""``) already answered in the turn: not notified again, except
  when a restart orphaned it (cause ``gateway_restarted``), which nobody else will report.
- Wonhee jobs are reported by the creative bridge after the real delivery: skipped here.
- A parent that started child jobs lets the children report: its ``done`` is skipped.
- Never resend: a message that was being sent when the process died is marked uncertain.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.kernel.job_report import format_job_notice
from nanobot.kernel.jobs import (
    NOTIFY_SENDING,
    NOTIFY_SENT,
    NOTIFY_SKIPPED,
    NOTIFY_UNCERTAIN,
    FailureSide,
    Job,
    JobState,
    JobStore,
    Notification,
)

RESTART_CAUSE = "gateway_restarted"

Deliver = Callable[..., Awaitable[None]]


def decide(job: Job, state: JobState, cause: str | None, has_children: bool) -> str:
    """``"send"`` or ``"skip"`` for one notification."""
    if job.role == "wonhee":
        return "skip"
    if job.role == "":
        return "send" if cause == RESTART_CAUSE else "skip"
    if has_children and state is JobState.DONE:
        return "skip"
    return "send"


def format_notification(job: Job, state: JobState, store: JobStore) -> str:
    events = store.history(job.id)
    who = {"worker": "Worker"}.get(job.role, "Job")
    head = f"{who} update. Your request: {job.request_text[:140]!r}"
    return f"{head}\n{format_job_notice(job, events)}"


class JobNotifier:
    def __init__(
        self, store: JobStore, deliver: Deliver,
        fallback_target: Callable[[], tuple[str, str]] | None = None,
        *, poll_s: float = 5.0,
    ) -> None:
        self.store = store
        self._deliver = deliver
        self._fallback = fallback_target
        self._poll_s = poll_s
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.store.add_listener(self._on_transition)
        self.store.recover_notifications()
        self._orphan_active_chat_jobs()
        self._task = asyncio.create_task(self._run(), name="moeka-job-notifier")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _on_transition(self, job_id: str, state: JobState) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    def _orphan_active_chat_jobs(self) -> None:
        """Jobs left active by a previous process can never finish: say so, once."""
        for job in self.store.active():
            if job.role != "" or job.state is JobState.NEEDS_ANSWER:
                continue  # a waiting question can still be answered
            try:
                self.store.transition(
                    job.id, JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause=RESTART_CAUSE,
                    detail="the gateway restarted while this request was in progress",
                )
            except Exception:
                logger.exception("could not orphan job {}", job.id)

    # -- delivery -------------------------------------------------------------------

    def _target(self, job: Job) -> tuple[str, str, str] | None:
        if job.route is not None:
            return job.route
        if self._fallback is not None:
            channel, chat = self._fallback()
            return channel, chat, ""
        return None

    async def flush(self) -> int:
        """Send what is pending. Returns how many were sent."""
        sent = 0
        for note in self.store.pending_notifications():
            sent += await self._one(note)
        return sent

    async def _one(self, note: Notification) -> int:
        job = self.store.get(note.job_id)
        if job is None:
            self.store.set_notification(note.job_id, note.event_seq, NOTIFY_SKIPPED)
            return 0
        events = self.store.history(job.id)
        cause = next((e.cause for e in reversed(events) if e.state is note.state), None)
        if decide(job, note.state, cause, bool(self.store.children(job.id))) == "skip":
            self.store.set_notification(job.id, note.event_seq, NOTIFY_SKIPPED)
            return 0
        target = self._target(job)
        if target is None:
            logger.warning("no route to tell the owner about job {}", job.id)
            self.store.set_notification(job.id, note.event_seq, NOTIFY_SKIPPED)
            return 0
        channel, chat_id, thread = target
        meta = {"message_thread_id": int(thread)} if thread.isdigit() else {}
        self.store.set_notification(job.id, note.event_seq, NOTIFY_SENDING)
        try:
            await self._deliver(
                OutboundMessage(
                    channel=channel, chat_id=chat_id,
                    content=format_notification(job, note.state, self.store), metadata=meta,
                ),
                record=True,
            )
        except Exception:
            # It may have gone out: never auto-resend.
            logger.exception("owner notification for job {} is uncertain", job.id)
            self.store.set_notification(job.id, note.event_seq, NOTIFY_UNCERTAIN)
            return 0
        self.store.set_notification(job.id, note.event_seq, NOTIFY_SENT)
        return 1

    async def _run(self) -> None:
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job notifier pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
