"""Plain-text reports built from the job record (spec 015 phase 3).

The owner must never be left with "I completed the tool steps but couldn't produce a final
answer". These formatters turn a job and its events into what actually happened: the state,
which side stopped it and why, and real timings. Stdlib only (kernel import rule).
"""

from __future__ import annotations

from datetime import datetime

from nanobot.kernel.jobs import FailureSide, Job, JobEvent, JobState

_HEADLINE = {
    JobState.HEARD: "Heard, not started",
    JobState.NEEDS_ANSWER: "Waiting on your answer",
    JobState.WORKING: "Still working",
    JobState.BLOCKED: "Blocked",
    JobState.DONE: "Done",
    JobState.FAILED: "Failed",
    JobState.OUTCOME_UNKNOWN: "Outcome unknown",
}

_SIDE = {
    FailureSide.MOEKA: "moeka itself",
    FailureSide.WRAPPER: "a moeka wrapper",
    FailureSide.WONHEE: "wonhee",
    FailureSide.STAGECRAFT: "stagecraft",
    FailureSide.TELEGRAM: "telegram delivery",
    FailureSide.POLICY: "the access policy",
    FailureSide.WORKER: "the worker agent",
}


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _span(start: str, end: str) -> str:
    a, b = _parse(start), _parse(end)
    if not a or not b:
        return ""
    secs = max(0.0, (b - a).total_seconds())
    if secs < 90:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f}m" if secs < 5400 else f"{secs / 3600:.1f}h"


def _stopping_event(events: list[JobEvent]) -> JobEvent | None:
    for ev in reversed(events):
        if ev.cause:
            return ev
    return None


def format_job_notice(job: Job, events: list[JobEvent]) -> str:
    """One short message about where a job stands, with side, cause and timing when known."""
    lines = [f"{_HEADLINE.get(job.state, job.state.value)} (job {job.id})"]
    stop = _stopping_event(events)
    if job.state in (JobState.FAILED, JobState.BLOCKED, JobState.OUTCOME_UNKNOWN) and stop:
        who = _SIDE.get(stop.side, "unknown") if stop.side else "unknown"
        lines.append(f"Stopped by: {who}. Cause: {stop.cause}.")
    last = events[-1] if events else None
    if last and last.detail:
        lines.append(last.detail.strip()[:600])
    if events:
        took = _span(events[0].ts, events[-1].ts)
        if took:
            lines.append(f"Time from request to this state: {took}.")
    return "\n".join(lines)


def format_dead_turn_report(job: Job, events: list[JobEvent]) -> str:
    """What to send when a turn ended without a final answer, built by the system, not the model."""
    body = format_job_notice(job, events)
    if job.state is JobState.OUTCOME_UNKNOWN:
        tail = (
            "I cannot tell whether the work completed. I will not guess: "
            f"say 'status {job.id}' or ask me to check before you rely on it."
        )
    else:
        tail = f"Ask me for status on job {job.id} at any time."
    return (
        "I did not finish this turn with an answer. Here is what the record shows.\n"
        f"{body}\n{tail}"
    )


def format_ack(job: Job) -> str:
    return f"Heard. Working on it (job {job.id}). Ask for status any time."
