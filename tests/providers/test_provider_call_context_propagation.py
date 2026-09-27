"""``ProviderCallContext.request`` (native response_format) must survive every
place a context gets rebuilt rather than passed through untouched: the
FallbackProvider's primary-call and fallback-call context rebuilds, and the
base retry loop's image-stripped-context rebuild. Each of these used to build
a fresh ``ProviderCallContext(...)`` field by field and silently dropped
``request`` — this pins ``dataclasses.replace`` (or an explicit ``request=``)
at every site instead.
"""

from __future__ import annotations

from typing import Any

from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    ProviderConversationState,
    RequestExtras,
)
from nanobot.providers.fallback_provider import FallbackProvider
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

_RF = {"type": "json_object"}


class _StubProvider(LLMProvider):
    """Minimal concrete LLMProvider for FallbackProvider wiring."""

    def __init__(self, provider_name: str, respond: Any):
        super().__init__(provider_name=provider_name)
        self.calls: list[ProviderCallContext | None] = []
        self._respond = respond

    def get_default_model(self) -> str:
        return f"{self.provider_name}-model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs.get("provider_context"))
        return self._respond


class _FailingPreset:
    """Just enough of a model-preset shape for FallbackProvider's fallback loop."""

    def __init__(self, model: str):
        self.model = model
        self.max_tokens = 100
        self.temperature = 0.5
        self.context_window_tokens = None
        self.reasoning_effort = None


# ---------------------------------------------------------------------------
# FallbackProvider._primary_call_context
# ---------------------------------------------------------------------------


def test_primary_call_context_preserves_request():
    primary = _StubProvider("primary", LLMResponse(content="unused", finish_reason="stop"))
    fp = FallbackProvider(primary, [], provider_factory=lambda preset: primary)
    ctx = ProviderCallContext(request=RequestExtras(response_format=_RF))

    result = fp._primary_call_context(ctx, None)

    assert result.request is not None
    assert result.request.response_format == _RF


# ---------------------------------------------------------------------------
# FallbackProvider full fallback path (primary fails, fallback serves)
# ---------------------------------------------------------------------------


async def test_fallback_call_preserves_request_extras():
    primary = _StubProvider(
        "primary",
        LLMResponse(
            content="timeout", finish_reason="error", error_kind="timeout",
            error_should_retry=True,
        ),
    )
    fallback = _StubProvider("fallback", LLMResponse(content="ok", finish_reason="stop"))
    fp = FallbackProvider(
        primary, [_FailingPreset("fallback-model")], provider_factory=lambda preset: fallback,
    )
    ctx = ProviderCallContext(request=RequestExtras(response_format=_RF))

    response = await fp.chat(
        messages=[{"role": "user", "content": "hi"}], provider_context=ctx,
    )

    assert response.content == "ok"
    [seen_ctx] = fallback.calls
    assert seen_ctx is not None
    assert seen_ctx.request is not None
    assert seen_ctx.request.response_format == _RF


# ---------------------------------------------------------------------------
# base.py retry loop's image-stripped-context rebuild
# ---------------------------------------------------------------------------


async def test_retry_stripped_context_preserves_request_extras(monkeypatch):
    provider = OpenAICompatProvider(api_key="k", default_model="test-model")
    seen_contexts: list[ProviderCallContext | None] = []

    async def fake_chat(self, **kwargs: Any) -> LLMResponse:
        seen_contexts.append(kwargs.get("provider_context"))
        # Non-transient so the retry loop takes the image-strip path, not the
        # ordinary transient-retry path.
        return LLMResponse(content="Error: bad request", finish_reason="error",
                            error_should_retry=False)

    monkeypatch.setattr(OpenAICompatProvider, "chat", fake_chat)

    state = ProviderConversationState(kind="k", provider="p", model="m", version=1)
    ctx = ProviderCallContext(conversation_state=state, request=RequestExtras(response_format=_RF))
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]

    response = await provider.chat_with_retry(messages=messages, provider_context=ctx)

    assert response.content == "Error: bad request"
    assert len(seen_contexts) == 2  # original attempt, then the stripped retry
    stripped = seen_contexts[1]
    assert stripped is not None
    assert stripped.conversation_state is None  # dropped, as intended
    assert stripped.request is not None  # ...but request must survive
    assert stripped.request.response_format == _RF
