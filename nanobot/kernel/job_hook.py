"""Run hook that keeps the job record in step with an agent run (spec 015 phase 1).

``before_run`` moves the job to ``working``. ``after_run`` / ``on_finally`` map the runner's
stop reason to a terminal or waiting state, naming the failing side and cause so a dead turn
is never just "couldn't produce a final answer". Hook failures never break the run.
"""

from __future__ import annotations

import re

from loguru import logger

from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
)
from nanobot.kernel.jobs import FailureSide, JobState, JobStore

_DENIAL_PREFIXES = (
    "workspace_violation", "policy_denial", "exec_guard_denial", "ssrf_violation",
    "strict_sandbox",
)


def _denial_cause(events: list[dict[str, str]]) -> tuple[str, str] | None:
    """(cause, detail) of the last tool error that was a policy denial, if any."""
    for event in reversed(events):
        if event.get("status") != "error":
            continue
        detail = event.get("detail", "")
        for prefix in _DENIAL_PREFIXES:
            if detail.startswith(prefix):
                return prefix, f"{event.get('name', 'tool')}: {detail}"
    return None


def _last_error(events: list[dict[str, str]]) -> dict[str, str] | None:
    for event in reversed(events):
        if event.get("status") == "error":
            return event
    return None


_ERROR_CODE_RE = re.compile(r'"code":\s*"([a-z_]{1,40})"')


def _error_code(detail: str) -> str | None:
    """The structured error code a wonhee/stagecraft MCP tool put in its error body, if any."""
    match = _ERROR_CODE_RE.search(detail or "")
    return match.group(1) if match else None


def _side_for_tool(name: str) -> FailureSide:
    if name.startswith("mcp_wonhee"):
        return FailureSide.WONHEE
    if name.startswith("mcp_stagecraft"):
        return FailureSide.STAGECRAFT
    return FailureSide.MOEKA


def _summary(context: AgentRunHookContext) -> str:
    ran = list(dict.fromkeys(context.tools_used))[:8]
    err = _last_error(context.tool_events)
    parts = [f"tools run: {', '.join(ran) if ran else 'none'}"]
    if err:
        parts.append(f"last error: {err.get('name', 'tool')}: {err.get('detail', '')[:200]}")
    return "; ".join(parts)


class JobHook(AgentHook):
    """Drive one job's state from one run."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        super().__init__()
        self._store = store
        self._job_id = job_id
        self._settled = False

    def _set(self, state: JobState, **kw) -> None:
        try:
            self._store.transition(self._job_id, state, **kw)
        except Exception:
            logger.exception("job record update failed for {}", self._job_id)

    async def before_run(self, context: AgentRunHookContext) -> None:
        self._settled = False
        self._set(JobState.WORKING, detail="run started")

    async def after_iteration(self, context: AgentHookContext) -> None:
        # Record denials as they happen so the cause survives a crash mid-run.
        denial = _denial_cause(context.tool_events)
        if denial and context.tool_events and context.tool_events[-1].get("status") == "error":
            cause, detail = denial
            self._set(JobState.WORKING, side=FailureSide.POLICY, cause=cause, detail=detail)

    async def after_run(self, context: AgentRunHookContext) -> None:
        stop = context.stop_reason or "completed"
        summary = _summary(context)
        denial = _denial_cause(context.tool_events)
        if stop == "completed":
            self._set(JobState.DONE, detail=summary)
        elif stop == "ask_user":
            self._set(JobState.NEEDS_ANSWER, detail=summary)
        elif stop == "policy_denials" or (stop == "tool_error" and denial):
            cause, detail = denial or ("policy_denial", summary)
            self._set(JobState.BLOCKED, side=FailureSide.POLICY, cause=cause, detail=detail)
        elif stop == "tool_error":
            err = _last_error(context.tool_events) or {}
            name = err.get("name", "tool")
            code = _error_code(err.get("detail", ""))
            self._set(
                JobState.FAILED, side=_side_for_tool(name),
                cause=f"{name}:{code}" if code else f"tool_error:{name}", detail=summary,
            )
        elif stop == "empty_final_response":
            self._set(
                JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause="empty_final_response",
                detail=summary,
            )
        elif stop == "cancelled":
            self._set(
                JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause="cancelled", detail=summary,
            )
        else:  # error, max_iterations, deadline, budget, anything new
            self._set(JobState.FAILED, side=FailureSide.MOEKA, cause=stop, detail=summary)
        self._settled = True

    async def on_finally(self, context: AgentRunHookContext) -> None:
        if self._settled:
            return
        exc = context.exception
        cause = type(exc).__name__ if exc is not None else "run_ended_without_result"
        self._set(
            JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause=cause,
            detail=_summary(context),
        )
        self._settled = True


# Turns that are not an owner request: they never get a job.
_NON_REQUEST_CHANNELS = frozenset({"system", "cron", "heartbeat", "dream"})
_NON_REQUEST_KEY_PREFIXES = ("heartbeat", "dream:", "cron:")


def job_hook_factory(store: JobStore, *, role: str = ""):
    """Turn-hook factory: one job per owner request, resuming a job waiting on an answer."""

    def factory(ctx: AgentTurnHookContext) -> JobHook | None:
        text = (ctx.request_text or "").strip()
        key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"
        if (
            ctx.ephemeral
            or not text
            or ctx.channel in _NON_REQUEST_CHANNELS
            or key.startswith(_NON_REQUEST_KEY_PREFIXES)
        ):
            return None
        try:
            waiting = store.latest_waiting(key)
            job_id = waiting.id if waiting else store.open_job(
                session_key=key, request_text=text, role=role,
            ).id
        except Exception:
            logger.exception("could not open a job record for {}", key)
            return None
        return JobHook(store, job_id)

    return factory
