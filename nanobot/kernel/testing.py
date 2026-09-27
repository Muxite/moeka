"""Test doubles for the LLM layer: :class:`FakeProvider` scripts model replies.

Register one on a kernel to run ``kernel.llm`` (and later agents) offline::

    fake = FakeProvider(["hello", reply("cut", finish_reason="length")])
    kernel.llm.register_provider("main", fake, ModelSpec(name="main", model="m",
                                                          provider="fake"))

Each script item is consumed by one physical call:

- a ``str``: a normal reply with that text (and a small reported usage);
- an ``LLMResponse``: returned as is (see :func:`reply` and :func:`error`);
- an exception instance: raised from the provider call (the base class turns it
  into an error response, as for a real SDK exception);
- a callable ``fn(call) -> item`` (sync or async), called with the
  :class:`FakeCall`, whose result is handled as above.

When the script runs out, ``default`` answers (``None`` = an error that fails
the test loudly). Every call is recorded on :attr:`FakeProvider.calls`.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ProviderCallContext,
)


@dataclass
class FakeCall:
    """One recorded provider call."""

    messages: list[dict[str, Any]]
    kwargs: dict[str, Any]
    provider_context: ProviderCallContext | None = None
    stream: bool = False
    cancelled: bool = field(default=False, compare=False)


def reply(
    text: str,
    *,
    finish_reason: str = "stop",
    usage: LLMUsage | None = None,
    input_tokens: int = 10,
    output_tokens: int | None = None,
    reasoning: str | None = None,
) -> LLMResponse:
    """A successful response; usage defaults to a small reported count."""
    if usage is None:
        usage = LLMUsage.reported(
            input_tokens=input_tokens,
            output_tokens=output_tokens if output_tokens is not None else max(1, len(text) // 4),
        )
    return LLMResponse(
        content=text, finish_reason=finish_reason, usage=usage, reasoning_content=reasoning,
    )


def error(
    status: int | None = None,
    message: str = "fake provider error",
    *,
    kind: str | None = None,
    error_type: str | None = None,
    error_code: str | None = None,
    retry_after: float | None = None,
    should_retry: bool | None = None,
) -> LLMResponse:
    """An error response shaped like the providers' own (``finish_reason="error"``)."""
    return LLMResponse(
        content=message,
        finish_reason="error",
        error_status_code=status,
        error_kind=kind,
        error_type=error_type,
        error_code=error_code,
        error_retry_after_s=retry_after,
        error_should_retry=should_retry,
    )


Script = str | LLMResponse | BaseException | Callable[["FakeCall"], Any]


class FakeProvider(LLMProvider):
    """An ``LLMProvider`` that answers from a script and records every call.

    ``delay`` (seconds) is awaited before each reply (and between stream deltas),
    so deadlines can cancel a call; a cancelled call is marked ``cancelled`` on
    its :class:`FakeCall`. Streaming sends the reply text in
    ``chunk_size``-character deltas.
    """

    # Every sampling field and extra_body pass (nothing is dropped).
    supported_sampling_fields = frozenset({
        "temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty",
        "repetition_penalty", "logit_bias", "seed", "stop", "max_tokens", "reasoning_effort",
        "extra_body",
    })
    # Tests should not wait out the real retry schedule.
    _CHAT_RETRY_DELAYS = (0, 0, 0)

    def __init__(
        self,
        script: Iterable[Script] = (),
        *,
        default: Script | None = None,
        model: str = "fake-model",
        provider_name: str = "fake",
        delay: float = 0.0,
        chunk_size: int = 4,
    ) -> None:
        super().__init__(provider_name=provider_name)
        self._script: deque[Script] = deque(script)
        self.default = default
        self.model = model
        self.delay = delay
        self.chunk_size = max(1, chunk_size)
        self.calls: list[FakeCall] = []
        self.closed = False

    def push(self, *items: Script) -> None:
        """Append items to the script."""
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        self.closed = True

    async def _answer(self, call: FakeCall) -> LLMResponse:
        self.calls.append(call)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            call.cancelled = True
            raise
        if self._script:
            item: Any = self._script.popleft()
        elif self.default is not None:
            item = self.default
        else:
            raise RuntimeError("FakeProvider script exhausted")
        if callable(item) and not isinstance(item, BaseException):
            item = item(call)
            if inspect.isawaitable(item):
                item = await item
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return reply(item)
        if isinstance(item, LLMResponse):
            return copy.copy(item)
        raise TypeError(f"unsupported FakeProvider script item: {item!r}")

    @staticmethod
    def _record(
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
        context: ProviderCallContext | None,
        stream: bool,
    ) -> FakeCall:
        return FakeCall(
            messages=messages, kwargs=kwargs, provider_context=context, stream=stream,
        )

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return await self._answer(self._record(messages, kwargs, None, False))

    async def chat_with_context(
        self, *, provider_context: ProviderCallContext, **kwargs: Any,
    ) -> LLMResponse:
        messages = kwargs.pop("messages")
        return await self._answer(self._record(messages, kwargs, provider_context, False))

    async def _stream(
        self,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
        context: ProviderCallContext | None,
    ) -> LLMResponse:
        on_delta: Callable[[str], Awaitable[None]] | None = kwargs.pop("on_content_delta", None)
        kwargs.pop("on_thinking_delta", None)
        kwargs.pop("on_tool_call_delta", None)
        kwargs.pop("on_stream_recover", None)
        response = await self._answer(self._record(messages, kwargs, context, True))
        text = response.content or ""
        if on_delta is not None and response.finish_reason != "error":
            call = self.calls[-1]
            try:
                for start in range(0, len(text), self.chunk_size):
                    await on_delta(text[start:start + self.chunk_size])
                    await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                call.cancelled = True
                raise
        return response

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return await self._stream(messages, kwargs, None)

    async def chat_stream_with_context(
        self, *, provider_context: ProviderCallContext, **kwargs: Any,
    ) -> LLMResponse:
        messages = kwargs.pop("messages")
        return await self._stream(messages, kwargs, provider_context)


__all__ = ["FakeCall", "FakeProvider", "error", "reply"]
