"""Job-record writes for the wonhee seam (spec 015 phase 1).

The bridge feeds what it observes into the shared ``JobStore``: wonhee submissions (from
``health.active_jobs`` and ``delivery_show.failures``), their output deliveries, and call
failures classified by side. Pure bookkeeping: every method swallows its own errors so the
delivery loop never stops on a record write.
"""

from __future__ import annotations

import functools
from typing import Any

from loguru import logger

from nanobot.kernel.jobs import FailureSide, Job, JobState, JobStore

REF_JOB = "wonhee_job"
REF_DELIVERY = "wonhee_delivery"
_TERMINAL = frozenset({JobState.DONE, JobState.FAILED})


def classify_call_error(exc: BaseException) -> tuple[FailureSide, str]:
    """(side, cause) for a failed wonhee call, so the owner learns which side broke."""
    code = getattr(exc, "code", None)
    if code in {"timeout", "invalid_output"}:
        return FailureSide.WRAPPER, str(code)  # the link (subprocess, JSON), not wonhee's logic
    if isinstance(code, str):
        return FailureSide.WONHEE, code
    return FailureSide.WRAPPER, type(exc).__name__


def classify_delivery_error(exc: BaseException) -> tuple[FailureSide, str]:
    """(side, cause) for a failed Telegram send of a wonhee output."""
    if isinstance(exc, FileNotFoundError):
        return FailureSide.WONHEE, "output_file_missing"
    if isinstance(exc, ValueError):
        return FailureSide.TELEGRAM, "invalid_delivery"
    return FailureSide.TELEGRAM, type(exc).__name__


def _guarded(fn):
    """A job-record failure is logged, never raised into the delivery loop."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("job record write failed in {}", fn.__name__)
            return None

    return wrapper


class WonheeJobTracker:
    def __init__(self, store: JobStore | None) -> None:
        self.store = store
        self._sig: dict[str, tuple[Any, ...]] = {}

    # -- helpers --------------------------------------------------------------------

    def _job_for(self, wonhee_id: object, route: object, project_id: object, *, create: bool) -> Job | None:
        assert self.store is not None
        job = self.store.find_by_ref(REF_JOB, str(wonhee_id))
        if job is not None or not create:
            return job
        chat = route.get("chat_id") if isinstance(route, dict) else None
        job = self.store.open_job(
            session_key=f"telegram:{chat}" if chat else "wonhee",
            request_text=f"wonhee job {wonhee_id} (project {project_id})",
            role="wonhee",
        )
        self.store.link_ref(job.id, REF_JOB, str(wonhee_id))
        return job

    def _write(self, fn, *args, **kw) -> None:
        try:
            fn(*args, **kw)
        except Exception:
            logger.exception("job record write failed")

    # -- observations ---------------------------------------------------------------

    @_guarded
    def observe_health(self, active_jobs: list[dict[str, Any]]) -> None:
        if self.store is None:
            return
        seen: set[str] = set()
        for item in active_jobs:
            jid = item.get("id")
            if jid is None:
                continue
            seen.add(str(jid))
            stalled = bool(item.get("stalled"))
            sig = (item.get("status"), item.get("phase"), stalled)
            if self._sig.get(str(jid)) == sig:
                continue
            self._sig[str(jid)] = sig
            detail = f"wonhee {item.get('status')}" + (
                f" in {item['phase']}" if item.get("phase") else "")
            try:
                job = self._job_for(jid, item.get("route"), item.get("project_id"), create=True)
                assert job is not None
                if stalled:
                    detail += f", idle {round(float(item.get('idle_s') or 0) / 60)} min"
                    self.store.transition(job.id, JobState.WORKING, side=FailureSide.WONHEE,
                                          cause="stalled", detail=detail)
                else:
                    self.store.transition(job.id, JobState.WORKING, detail=detail)
            except Exception:
                logger.exception("job record write failed")
        # A tracked job that left the active list is neither done nor failed yet: say so once.
        for job in self.store.active():
            wid = job.refs.get(REF_JOB)
            if wid is None or wid in seen or self._sig.get(wid) == ("gone",):
                continue
            self._sig[wid] = ("gone",)
            self._write(self.store.transition, job.id, JobState.WORKING,
                        detail="no longer in wonhee's active list; waiting for its output or failure")

    @_guarded
    def observe_failures(self, failures: list[dict[str, Any]]) -> None:
        if self.store is None:
            return
        for failure in failures:
            job = self._job_for(failure.get("id"), None, None, create=False)
            if job is None or job.state in _TERMINAL:
                continue  # history we never tracked, or already recorded
            self._write(
                self.store.transition, job.id, JobState.FAILED, side=FailureSide.WONHEE,
                cause="submission_failed", detail=str(failure.get("error") or "no reason given"),
            )

    @_guarded
    def observe_delivery_ready(self, delivery: dict[str, Any]) -> None:
        """A finished output is queued for delivery: link it, but wait for the send to say done."""
        if self.store is None or delivery.get("submission_id") is None:
            return
        job = self._job_for(delivery["submission_id"], None, None, create=False)
        if job is None or job.state in _TERMINAL:
            return
        self._write(self.store.link_ref, job.id, REF_DELIVERY, str(delivery.get("id")))
        self._write(self.store.transition, job.id, JobState.WORKING,
                    detail=f"output ready: {delivery.get('filename')}; delivering")

    @_guarded
    def delivery_sent(self, delivery: dict[str, Any], message_ids: list[int]) -> None:
        job = self._delivery_job(delivery)
        if job is not None:
            self._write(self.store.transition, job.id, JobState.DONE,
                        detail=f"delivered {delivery.get('filename')} (telegram message {message_ids})")

    @_guarded
    def delivery_failed(self, delivery: dict[str, Any], exc: BaseException, *, uncertain: bool) -> None:
        job = self._delivery_job(delivery)
        if job is None:
            return
        side, cause = classify_delivery_error(exc)
        state = JobState.OUTCOME_UNKNOWN if uncertain else JobState.FAILED
        self._write(self.store.transition, job.id, state, side=side, cause=cause,
                    detail=f"sending {delivery.get('filename')}: {type(exc).__name__}")

    def _delivery_job(self, delivery: dict[str, Any]) -> Job | None:
        if self.store is None:
            return None
        if delivery.get("submission_id") is not None:
            job = self._job_for(delivery["submission_id"], None, None, create=False)
            if job is not None:
                return job
        return self.store.find_by_ref(REF_DELIVERY, str(delivery.get("id")))

    @_guarded
    def intake_failed(self, route_key: str, receipt_id: str, exc: BaseException) -> None:
        """An owner input could not be imported into wonhee after retries."""
        if self.store is None:
            return
        side, cause = classify_call_error(exc)
        try:
            job = self.store.open_job(
                session_key=route_key, request_text=f"intake {receipt_id[:12]}", role="wonhee",
            )
            self.store.transition(job.id, JobState.FAILED, side=side, cause=cause, detail=str(exc))
        except Exception:
            logger.exception("job record write failed")
