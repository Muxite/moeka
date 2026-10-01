"""Trace contract for the kernel: sinks, spans, the stamping ``safe_emit`` and ``Tracer``.

Events are plain dicts with an ``"event"`` key (see :data:`EVENTS` for every name the
kernel emits and its payload keys).

Spans (context variables, so they work in sync code, asyncio tasks and threads that
copy the context, e.g. the kernel loop bridge):
- :func:`span` (``Tracer.span``) is a sync *and* async context manager. A root span
  (no active trace) mints a trace id (``uuid4().hex``); a nested span keeps the trace
  id, extends the span path (``"rollout/select"``) and merges its tags over the
  outer span's (inner wins).
- Entering a span also binds ``llm_usage_trace_id`` to the trace id, so ledger
  ``model.call`` events carry it.
- Each asyncio task gets its own copy of the context: two concurrent tasks in
  different spans never see each other's tags.

Stamping: :func:`safe_emit` (and ``Tracer.emit``) deliver a *copy* of the event
stamped with ``trace_id`` (``None`` outside any span), ``span`` (the path, ``None``
outside any span), ``tags`` (the span tags, with an event's own ``tags`` mapping
merged over them) and ``ts`` (epoch seconds, ``time.time()``, a float). Keys the event
already sets are never overwritten. Stamping never raises.

:class:`Tracer` is the kernel's sink (``Kernel.trace``): it stamps, forwards to the
host's sink and fans out to subscribers, each isolated from the others.

Imports: stdlib and loguru only (``nanobot.llm_usage.context`` lazily); import-cheap.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class TraceSink(Protocol):
    """Receives structured trace events. Implementations should not raise."""

    def emit(self, event: dict[str, Any]) -> None: ...


class NullTraceSink:
    """Discards every event."""

    def emit(self, event: dict[str, Any]) -> None:
        return None


class LoguruTraceSink:
    """Writes each event as a debug loguru record."""

    def emit(self, event: dict[str, Any]) -> None:
        logger.debug("trace {}", event)


# -- span context -----------------------------------------------------------------------

_EMPTY: Mapping[str, Any] = MappingProxyType({})

_TRACE_ID: ContextVar[str | None] = ContextVar("moeka_trace_id", default=None)
_SPAN_PATH: ContextVar[str | None] = ContextVar("moeka_trace_span", default=None)
_SPAN_TAGS: ContextVar[Mapping[str, Any]] = ContextVar("moeka_trace_tags", default=_EMPTY)


def current_trace_id() -> str | None:
    """The active trace id (``None`` outside any span)."""
    return _TRACE_ID.get()


def current_span() -> str | None:
    """The active span path, e.g. ``"rollout/select"`` (``None`` outside any span)."""
    return _SPAN_PATH.get()


def current_tags() -> dict[str, Any]:
    """A copy of the active span's merged tags (empty outside any span)."""
    return dict(_SPAN_TAGS.get())


class span:  # noqa: N801 - used like a function: ``with span("stage", k=v):``
    """A trace span; a sync and async context manager (single use).

    ``trace_id``, ``path`` and ``tags`` are set on enter (``__enter__`` returns the
    span itself).
    """

    __slots__ = ("name", "_own_tags", "trace_id", "path", "tags", "_tokens")

    def __init__(self, name: str, /, **tags: Any) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"span name must be a non-empty string, got {name!r}")
        self.name = name
        self._own_tags = tags
        self.trace_id: str | None = None
        self.path: str | None = None
        self.tags: Mapping[str, Any] = _EMPTY
        self._tokens: tuple[Token[Any], ...] | None = None

    def __enter__(self) -> span:
        if self._tokens is not None:
            raise RuntimeError(f"span {self.name!r} is already active")
        from nanobot.llm_usage.context import bind_llm_usage_trace_id

        parent_id = _TRACE_ID.get()
        parent_path = _SPAN_PATH.get()
        self.trace_id = parent_id if parent_id is not None else uuid.uuid4().hex
        self.path = self.name if parent_path is None else f"{parent_path}/{self.name}"
        self.tags = MappingProxyType({**_SPAN_TAGS.get(), **self._own_tags})
        self._tokens = (
            _TRACE_ID.set(self.trace_id),
            _SPAN_PATH.set(self.path),
            _SPAN_TAGS.set(self.tags),
            bind_llm_usage_trace_id(self.trace_id),
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        from nanobot.llm_usage.context import reset_llm_usage_trace_id

        tokens, self._tokens = self._tokens, None
        if tokens is None:
            return
        trace_token, path_token, tags_token, usage_token = tokens
        reset_llm_usage_trace_id(usage_token)
        _SPAN_TAGS.reset(tags_token)
        _SPAN_PATH.reset(path_token)
        _TRACE_ID.reset(trace_token)

    async def __aenter__(self) -> span:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __repr__(self) -> str:
        return f"span({self.path or self.name!r}, trace_id={self.trace_id!r})"


def stamp(event: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of *event* stamped with ``trace_id``/``span``/``tags``/``ts``. Never raises.

    Keys the event already sets win; an event ``tags`` mapping is merged over the
    span tags.
    """
    try:
        stamped = dict(event)
        stamped.setdefault("trace_id", _TRACE_ID.get())
        stamped.setdefault("span", _SPAN_PATH.get())
        span_tags = _SPAN_TAGS.get()
        own = stamped.get("tags")
        if own is None:
            stamped["tags"] = dict(span_tags)
        elif isinstance(own, Mapping):
            stamped["tags"] = {**span_tags, **own}
        stamped.setdefault("ts", time.time())
        return stamped
    except Exception:  # noqa: BLE001 - stamping must never break the caller
        return event if isinstance(event, dict) else {"event": None}


def _log_sink_failure(sink: Any, exc: BaseException, event: Any) -> None:
    try:
        logger.warning("trace sink {} failed: {!r}; event={}", type(sink).__name__, exc, event)
    except Exception:  # noqa: BLE001
        pass


def safe_emit(sink: TraceSink, event: dict[str, Any]) -> None:
    """Stamp *event* (a copy; see :func:`stamp`) and emit it to *sink*; swallow and log
    any sink failure."""
    stamped = stamp(event)
    try:
        sink.emit(stamped)
    except Exception as exc:  # noqa: BLE001 - sinks must never break the caller
        _log_sink_failure(sink, exc, stamped)


# -- sinks --------------------------------------------------------------------------------


class MemoryTraceSink:
    """Keeps every event in memory (thread-safe); for tests and in-process analysis."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[dict[str, Any]]:
        """A snapshot of the events so far, in emit order."""
        with self._lock:
            return list(self._events)

    def of(self, event_type: str) -> list[dict[str, Any]]:
        """The events named *event_type*, in emit order."""
        with self._lock:
            return [e for e in self._events if e.get("event") == event_type]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class JsonlTraceSink:
    """Appends one JSON line per event to *path* (thread-safe, flushed per write).

    Values JSON cannot encode are written as ``str(value)``. The parent directory
    is created. Events emitted after :meth:`close` are dropped.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: Any = self.path.open("a", encoding="utf-8")

    def emit(self, event: dict[str, Any]) -> None:
        try:
            line = json.dumps(event, default=str, ensure_ascii=False)
        except (TypeError, ValueError) as exc:  # e.g. a circular reference
            _log_sink_failure(self, exc, event)
            return
        with self._lock:
            if self._fh is None:
                return
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        """Close the file. Idempotent."""
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()

    @property
    def closed(self) -> bool:
        return self._fh is None

    def __enter__(self) -> JsonlTraceSink:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class FanoutSink:
    """Emits every event to each of *sinks*; one failing sink never stops the others."""

    def __init__(self, *sinks: TraceSink) -> None:
        self.sinks: tuple[TraceSink, ...] = sinks

    def emit(self, event: dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # noqa: BLE001
                _log_sink_failure(sink, exc, event)


# -- tracer --------------------------------------------------------------------------------

Subscriber = Callable[[dict[str, Any]], Any]


class Tracer:
    """The kernel's trace sink: stamps events, forwards them to the host sink and to
    subscribers.

    - ``emit`` never raises: the host sink and every subscriber are isolated.
    - Each subscriber gets its own shallow copy of the stamped event.
    - ``subscribe`` / unsubscribe are thread-safe; delivery happens on the emitting
      thread (often the kernel loop thread), so subscribers should be quick.
    """

    def __init__(self, sink: TraceSink | None = None) -> None:
        self._sink: TraceSink = sink if sink is not None else NullTraceSink()
        self._lock = threading.Lock()
        self._subs: tuple[tuple[object, str | None, Subscriber], ...] = ()

    @property
    def sink(self) -> TraceSink:
        """The host sink events are forwarded to."""
        return self._sink

    def emit(self, event: dict[str, Any]) -> None:
        stamped = stamp(event)
        try:
            self._sink.emit(stamped)
        except Exception as exc:  # noqa: BLE001
            _log_sink_failure(self._sink, exc, stamped)
        subs = self._subs
        if not subs:
            return
        name = stamped.get("event")
        for _, event_type, fn in subs:
            if event_type is not None and event_type != name:
                continue
            try:
                fn(dict(stamped))
            except Exception as exc:  # noqa: BLE001 - one subscriber never breaks another
                _log_sink_failure(fn, exc, stamped)

    def subscribe(self, event_type: str | None, fn: Subscriber) -> Callable[[], None]:
        """Call *fn(event)* for every event named *event_type* (``None`` = all events).

        Returns an idempotent unsubscribe function.
        """
        if not callable(fn):
            raise TypeError(f"subscriber must be callable, got {type(fn).__name__}")
        if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
            getattr(fn, "__call__", None)
        ):
            raise TypeError(
                "subscriber must be a plain function: events are delivered synchronously "
                "and a coroutine would never run (hand work off to your own loop instead)"
            )
        key = object()
        with self._lock:
            self._subs = (*self._subs, (key, event_type, fn))

        def unsubscribe() -> None:
            with self._lock:
                self._subs = tuple(s for s in self._subs if s[0] is not key)

        return unsubscribe

    @staticmethod
    def span(name: str, /, **tags: Any) -> span:
        """A trace span (see :class:`span`); spans are per-context, not per-tracer."""
        return span(name, **tags)

    def __repr__(self) -> str:
        return f"Tracer(sink={type(self._sink).__name__}, subscribers={len(self._subs)})"


# -- event catalogue ------------------------------------------------------------------------

EVENTS: Mapping[str, str] = MappingProxyType({
    # every event also carries trace_id, span, tags and ts (see the module docstring)
    "policy.decision": "actor, principal_kind, tool, capability, resource, verdict "
                       "(allow|deny), layer, marker",
    "tool.invalid": "actor, principal_kind, tool, call_id (the model's tool-call id; "
                    "joins tool.call.call_id), error (unknown tool or invalid arguments)",
    "tool.result_invalid": "actor, principal_kind, tool, marker, error (result failed "
                           "the tool's output schema)",
    "tool.dropped": "tool, capabilities, reason (strict mode removed the tool)",
    "plugin.load": "verdict, plugin, entry_point, tool, grant, reason",
    "plugin.lifecycle": "verdict, action, plugin, from, to, version_hash, actor, "
                        "actor_kind, reason",
    "model.route": "slot, task_type, tier, preset, ceiling, reason, check, verdict, solver "
                   "(one model-router decision)",
    "model.call": "one provider call (ledger): trace_id, slot, tier, model, provider, "
                  "tokens_in, tokens_out, tokens_cache_read, latency_ms, cost_usd, source, "
                  "usage_source, finish_reason, call_id, alias, attempt, cached, tags; "
                  "usage-record.v1 fields: schema_version, record_id, kind, consumer, agent, "
                  "session, role, purpose, tokens_cache_write, tokens_reasoning, price_source, "
                  "cost_billed, cache_hit, outcome, error_kind, started_at_ms, request_key, "
                  "prompt_version, waste_label, waste_set_by, producer",
    "fact.recorded": "fact_id, source_kind, source_ref, source_span (the fact's "
                     "source character span)",
    "artifact.proposed": "artifact_id, kind, committed {path: fact id}, provisional",
    "artifact.rejected": "artifact_id, kind, reason (error class), paths",
    "paths.overlap_allowed": "work_dir, state_dir (legacy layout shares them)",
    "sampling.dropped": "provider, model, fields (sampling fields the provider rejected)",
    "budget.admit": "call_id, alias, model, provider, prompt_tokens, max_output_tokens, "
                    "rounds, worst_case_tokens, worst_case_usd, tags; budget-event.v1 fields: "
                    "schema_version, kind, consumer, agent, session, role, purpose, scope, "
                    "cap_*, spent_*, reserved_*, remaining_*, refusal",
    "budget.refuse": "budget.admit keys plus reason (refusal.code is the typed reason)",
    "cache.hit": "call_id, key (16-char prefix), alias, model, tags; usage-record.v1 fields "
                 "(kind cache_hit): schema_version, record_id, consumer, agent, session, role, "
                 "purpose, tokens_in/out 0, saved_tokens_in, saved_tokens_out, saved_cost_usd",
    "call.waste": "call_id, attempt, waste_label, waste_set_by (a caller-known waste label "
                  "applied to an earlier call, e.g. a discarded draft; append-only)",
    "run.started": "session_key, model (agent run began)",
    "iteration": "session_key, iteration, tool_calls, finish_reason, usage (this "
                 "iteration's token delta)",
    "tool.call": "session_key, iteration, tool, call_id, ok, args_valid (False for "
                 "invalid arguments / unknown tool), error_kind, error, duration_ms, "
                 "args_digest (sha256 hex of the canonical JSON of the arguments as the "
                 "model produced them, None when not canonicalisable; never raw arguments)",
    "run.completed": "session_key, model, stop_reason, iterations, usage (totals), "
                     "tools_used, error",
    "skill.listed": "skills (names rendered into the system prompt), active (always-on "
                    "skills loaded in full)",
    "skill.read": "skill, path (resolved), via (the tool: read_file or grep), call_id "
                  "(the model's tool-call id; joins tool.call.call_id; None outside a "
                  "runner). One per (tool call, skill file) whose successful result returned "
                  "SKILL.md content: read_file (any successful read) and grep in "
                  "output_mode=content; emitted before that call's tool.call. A lower bound: "
                  "reads through exec, exec_session, MCP or other tools are not seen",
    "mcp.error": "agent, server, error (an agent's MCP server failed to connect; the "
                 "agent runs without its tools)",
})


__all__ = [
    "EVENTS",
    "FanoutSink",
    "JsonlTraceSink",
    "LoguruTraceSink",
    "MemoryTraceSink",
    "NullTraceSink",
    "Tracer",
    "TraceSink",
    "current_span",
    "current_tags",
    "current_trace_id",
    "safe_emit",
    "span",
    "stamp",
]
