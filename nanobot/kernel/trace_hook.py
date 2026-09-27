"""``TraceHook``: an :class:`~nanobot.agent.hook.AgentHook` that turns an agent run
into trace events.

Events (every one also stamped with ``trace_id``/``span``/``tags``/``ts`` by
:func:`~nanobot.kernel.trace.safe_emit`, so a run inside ``kernel.trace.span(...)``
carries that span):

- ``run.started``: ``session_key``, ``model``.
- ``iteration``: ``session_key``, ``iteration`` (0-based), ``tool_calls`` (count),
  ``finish_reason``, ``usage`` (this iteration's tokens, ``None`` when unknown).
- ``tool.call``: ``session_key``, ``iteration``, ``tool``, ``call_id``, ``ok``,
  ``args_valid``, ``error_kind``, ``error`` (first 200 chars), ``duration_ms``.
  The runner only calls tool hooks for calls whose arguments validated and that
  passed the capability gate, so ``args_valid`` is always ``True`` here; invalid
  arguments are traced as ``tool.invalid`` and denials as ``policy.decision``.
  ``error_kind`` is the exception class name, or ``"tool_error"`` for an error
  result (``"result_invalid"`` when the result failed the tool's output schema).
- ``run.completed``: ``session_key``, ``model``, ``stop_reason``, ``iterations``,
  ``usage`` (run totals), ``tools_used``, ``error``. Emitted from ``on_finally``, so
  exactly once per run, including runs that raised or were cancelled.

Use one ``TraceHook`` per run (it keeps per-run counters). It never raises.
"""

from __future__ import annotations

import time
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.kernel.trace import TraceSink, safe_emit

_ERROR_CHARS = 200


def usage_dict(usage: Any) -> dict[str, Any] | None:
    """The token counts of an ``LLMUsage`` (``None`` when there is none)."""
    if usage is None:
        return None
    try:
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
        }
    except AttributeError:
        return None


class TraceHook(AgentHook):
    """Emit ``run.*``, ``iteration`` and ``tool.call`` events to *sink*."""

    def __init__(
        self, sink: TraceSink, *, session_key: str | None = None, model: str | None = None,
    ) -> None:
        super().__init__()
        self._sink = sink
        self._session_key = session_key
        self._model = model
        self._iterations = 0
        self._iteration = 0
        self._tool_started: dict[str, float] = {}

    def _emit(self, event: dict[str, Any]) -> None:
        safe_emit(self._sink, event)

    async def before_run(self, context: AgentRunHookContext) -> None:
        self._iterations = 0
        self._iteration = 0
        self._tool_started.clear()
        self._emit({
            "event": "run.started", "session_key": self._session_key, "model": self._model,
        })

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._iteration = context.iteration
        if self._session_key is None and context.session_key is not None:
            self._session_key = context.session_key

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._iterations = max(self._iterations, context.iteration + 1)
        response = context.response
        self._emit({
            "event": "iteration",
            "session_key": self._session_key,
            "iteration": context.iteration,
            "tool_calls": len(context.tool_calls),
            "finish_reason": getattr(response, "finish_reason", None),
            "usage": usage_dict(context.usage),
        })

    async def before_execute_tool(
        self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any,
    ) -> None:
        self._tool_started[self._call_key(tool_call)] = time.monotonic()

    async def after_execute_tool(
        self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any, result: Any,
    ) -> None:
        self._tool_event(context, tool_call, ok=True)

    async def on_execute_tool_error(
        self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any, error: Any,
    ) -> None:
        if isinstance(error, BaseException):
            kind = type(error).__name__
        else:
            from nanobot.kernel.typed import RESULT_SCHEMA_MARKER

            kind = "result_invalid" if RESULT_SCHEMA_MARKER in str(error) else "tool_error"
        self._tool_event(
            context, tool_call, ok=False, error_kind=kind, error=str(error)[:_ERROR_CHARS],
        )

    async def on_finally(self, context: AgentRunHookContext) -> None:
        error = context.error
        if error is None and context.exception is not None:
            error = f"{type(context.exception).__name__}: {context.exception}"[:_ERROR_CHARS]
        self._emit({
            "event": "run.completed",
            "session_key": self._session_key,
            "model": self._model,
            "stop_reason": context.stop_reason,
            "iterations": self._iterations,
            "usage": usage_dict(context.usage),
            "tools_used": list(context.tools_used),
            "error": error,
        })

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _call_key(tool_call: Any) -> str:
        return str(getattr(tool_call, "id", None) or id(tool_call))

    def _tool_event(
        self,
        context: AgentHookContext,
        tool_call: Any,
        *,
        ok: bool,
        error_kind: str | None = None,
        error: str | None = None,
    ) -> None:
        started = self._tool_started.pop(self._call_key(tool_call), None)
        duration = (time.monotonic() - started) * 1000.0 if started is not None else None
        self._emit({
            "event": "tool.call",
            "session_key": self._session_key,
            "iteration": context.iteration,
            "tool": getattr(tool_call, "name", None),
            "call_id": getattr(tool_call, "id", None),
            "ok": ok,
            "args_valid": True,
            "error_kind": error_kind,
            "error": error,
            "duration_ms": duration,
        })


__all__ = ["TraceHook", "usage_dict"]
