"""Streaming support for the high-level Python SDK."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.sdk.types import (
    STREAM_EVENT_REASONING_COMPLETED,
    STREAM_EVENT_REASONING_DELTA,
    STREAM_EVENT_RUN_FAILED,
    STREAM_EVENT_RUN_STARTED,
    STREAM_EVENT_TEXT_COMPLETED,
    STREAM_EVENT_TEXT_DELTA,
    STREAM_EVENT_TOOL_COMPLETED,
    STREAM_EVENT_TOOL_FAILED,
    STREAM_EVENT_TOOL_STARTED,
    StreamEvent,
)

if TYPE_CHECKING:
    from nanobot.utils.llm_runtime import LLMRuntime

_STREAM_SENTINEL = object()
_T = TypeVar("_T")


class RunStream(Generic[_T]):
    """A running SDK turn with Cursor/OpenAI-style event streaming.

    ``wait()`` returns the turn's result: an SDK :class:`RunResult` for ``Nanobot``,
    the kernel's ``RunResult`` for a kernel agent (see :func:`start_streamed_run`).
    """

    def __init__(
        self,
        task: asyncio.Task[_T],
        queue: asyncio.Queue[StreamEvent | object],
    ) -> None:
        self._task = task
        self._queue = queue
        self._events_started = False
        self._events_done = False
        self._stream_active = False
        self._closed = False

    @property
    def done(self) -> bool:
        """Whether the underlying run task has finished."""
        return self._task.done()

    async def stream_events(self) -> AsyncIterator[StreamEvent]:
        """Yield streaming events for this run.

        The event stream is single-consumer: call this method only once. Closing
        the iterator before completion cancels the underlying run.
        """
        if self._events_started:
            raise RuntimeError("RunStream.stream_events() can only be consumed once")
        self._events_started = True
        self._stream_active = True
        try:
            while True:
                item = await self._queue.get()
                if item is _STREAM_SENTINEL:
                    self._events_done = True
                    break
                if not isinstance(item, StreamEvent):
                    raise TypeError("SDK event queue contained an invalid item")
                yield item
        finally:
            self._stream_active = False
            if not self._events_done:
                await self.aclose()

    async def wait(self) -> _T:
        """Wait for the run to finish and return its final result."""
        if not self._events_done and not self._stream_active:
            if not self._events_started:
                self._events_started = True
            await self._drain_events()
        return await self._task

    async def text(self) -> str:
        """Wait for the run to finish and return the final text."""
        return (await self.wait()).content

    async def cancel(self) -> None:
        """Cancel the running turn and release stream resources."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the stream, cancelling the run if it is still active."""
        if self._closed:
            return
        self._closed = True
        if not self._task.done():
            self._task.cancel()
        self._finish_events()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Closing is cleanup; wait() remains the API that surfaces run errors.
            pass

    async def _drain_events(self) -> None:
        while not self._events_done:
            item = await self._queue.get()
            if item is _STREAM_SENTINEL:
                self._events_done = True
                break

    def _finish_events(self) -> None:
        self._events_done = True
        while True:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                continue
            break
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(_STREAM_SENTINEL)


class SDKStreamEmitter:
    """Serialize SDK streaming events onto a bounded async queue."""

    def __init__(self, queue: asyncio.Queue[StreamEvent | object]) -> None:
        self._queue = queue
        self._text_parts: list[str] = []
        self._closed = False

    async def emit(self, event: StreamEvent) -> None:
        if self._closed:
            return
        await self._queue.put(event)

    async def text_delta(self, delta: str, *, iteration: int | None = None) -> None:
        if not delta:
            return
        self._text_parts.append(delta)
        await self.emit(StreamEvent(
            type=STREAM_EVENT_TEXT_DELTA,
            delta=delta,
            iteration=iteration,
        ))

    async def text_completed(
        self,
        *,
        resuming: bool = False,
        iteration: int | None = None,
        force: bool = True,
    ) -> None:
        content = "".join(self._text_parts)
        if not content and (resuming or not force):
            return
        self._text_parts = []
        await self.emit(StreamEvent(
            type=STREAM_EVENT_TEXT_COMPLETED,
            content=content,
            iteration=iteration,
            resuming=resuming,
        ))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(_STREAM_SENTINEL)


class SDKStreamingHook(AgentHook):
    """Convert agent lifecycle hooks into public SDK stream events."""

    def __init__(self, emitter: SDKStreamEmitter) -> None:
        super().__init__()
        self._emitter = emitter
        self._reasoning_open = False

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            await self._emitter.emit(StreamEvent(
                type=STREAM_EVENT_TOOL_STARTED,
                name=call.name,
                tool_call_id=call.id,
                arguments=deepcopy(call.arguments),
                iteration=context.iteration,
            ))

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        if not reasoning_content:
            return
        self._reasoning_open = True
        await self._emitter.emit(StreamEvent(
            type=STREAM_EVENT_REASONING_DELTA,
            delta=reasoning_content,
        ))

    async def emit_reasoning_end(self) -> None:
        if not self._reasoning_open:
            return
        self._reasoning_open = False
        await self._emitter.emit(StreamEvent(type=STREAM_EVENT_REASONING_COMPLETED))

    async def after_iteration(self, context: AgentHookContext) -> None:
        if not context.tool_events:
            return
        for index, raw_event in enumerate(context.tool_events):
            call = context.tool_calls[index] if index < len(context.tool_calls) else None
            event = dict(raw_event)
            status = event.get("status")
            name = str(event.get("name") or (call.name if call else ""))
            event_type = (
                STREAM_EVENT_TOOL_COMPLETED if status == "ok" else STREAM_EVENT_TOOL_FAILED
            )
            await self._emitter.emit(StreamEvent(
                type=event_type,
                name=name or None,
                tool_call_id=call.id if call else None,
                arguments=deepcopy(call.arguments) if call else None,
                iteration=context.iteration,
                error=None if status == "ok" else str(event.get("detail") or ""),
                metadata=event,
            ))


class StreamWiring:
    """What one streamed turn adds to ``AgentLoop.process_direct``.

    Pass :meth:`process_kwargs` to ``process_direct`` and add :attr:`hook` to its
    ``hooks``: text deltas, reasoning, tool events and ``run.started`` (when the
    runtime is admitted) then reach the stream.
    """

    def __init__(
        self,
        emitter: SDKStreamEmitter,
        started_metadata: Mapping[str, Any],
        runtime: LLMRuntime | None,
    ) -> None:
        self.emitter = emitter
        self.hook = SDKStreamingHook(emitter)
        self._started_metadata = dict(started_metadata)
        self._runtime = runtime
        self._run_started = False

    async def emit_run_started(self, runtime: LLMRuntime | None = None) -> None:
        """Emit ``run.started`` once (with the admitted runtime's model when known)."""
        if self._run_started:
            return
        if runtime is None:
            runtime = self._runtime
        metadata = dict(self._started_metadata)
        if runtime is not None:
            metadata.update({
                "model": runtime.model,
                "model_preset": runtime.model_preset,
            })
        await self.emitter.emit(StreamEvent(
            type=STREAM_EVENT_RUN_STARTED,
            metadata=metadata,
        ))
        self._run_started = True

    async def _on_stream(self, delta: str) -> None:
        await self.emitter.text_delta(delta)

    async def _on_stream_end(self, *_args: Any, resuming: bool = False, **_kwargs: Any) -> None:
        await self.emitter.text_completed(resuming=resuming)

    def process_kwargs(self) -> dict[str, Any]:
        """``on_stream`` / ``on_stream_end`` / ``on_runtime_admitted`` for ``process_direct``."""
        return {
            "on_stream": self._on_stream,
            "on_stream_end": self._on_stream_end,
            "on_runtime_admitted": self.emit_run_started,
        }


def start_streamed_run(
    body: Callable[[StreamWiring], Awaitable[_T]],
    *,
    final_event: Callable[[_T], StreamEvent],
    started_metadata: Mapping[str, Any],
    runtime: LLMRuntime | None = None,
) -> RunStream[_T]:
    """Start one streamed turn as a task on the running loop and return its handle.

    *body* runs the turn: it calls ``process_direct`` on its loop with the wiring's
    :meth:`~StreamWiring.process_kwargs` and :attr:`~StreamWiring.hook` added, and
    returns the turn's result. Around it this emits ``run.started`` (if the loop never
    admitted a runtime), a final ``text.completed`` for unflushed text, then
    ``final_event(result)`` (``run.completed``, or ``run.failed`` for a result that
    reports a failure), or ``run.failed`` when *body* raises; the stream always ends.
    ``Nanobot.run_streamed`` and the kernel ``Agent.stream`` both run through this.
    """
    queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue(maxsize=256)
    emitter = SDKStreamEmitter(queue)
    wiring = StreamWiring(emitter, started_metadata, runtime)

    async def _run() -> _T:
        try:
            result = await body(wiring)
            await wiring.emit_run_started()
            await emitter.text_completed(resuming=False, force=False)
            await emitter.emit(final_event(result))
            return result
        except Exception as exc:
            await wiring.emit_run_started()
            await emitter.emit(StreamEvent(
                type=STREAM_EVENT_RUN_FAILED,
                error=str(exc),
                metadata={"exception_type": type(exc).__name__},
            ))
            raise
        finally:
            await emitter.close()

    return RunStream(asyncio.create_task(_run()), queue)
