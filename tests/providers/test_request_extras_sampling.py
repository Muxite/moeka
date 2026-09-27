"""RequestExtras sampling / extra_body / on_unsupported through real provider builders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.kernel.llm_errors import UnsupportedRequestError
from nanobot.kernel.sampling import Sampling
from nanobot.providers.anthropic_provider import AnthropicProvider
from nanobot.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    RequestExtras,
)
from nanobot.providers.fallback_provider import FallbackProvider
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name

EVERY_FIELD = Sampling(
    temperature=0.2,
    top_p=0.9,
    top_k=40,
    min_p=0.05,
    presence_penalty=0.1,
    frequency_penalty=0.2,
    repetition_penalty=1.1,
    logit_bias={50256: -100},
    seed=7,
    stop=["END"],
    max_tokens=256,
    reasoning_effort="low",
)
MESSAGES = [{"role": "user", "content": "hi"}]


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _openai_client(calls: list[dict[str, Any]]) -> Any:
    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            )],
            usage=None,
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _anthropic_client(calls: list[dict[str, Any]]) -> Any:
    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=None,
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _openrouter(extra_body: dict[str, Any] | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="k",
        default_model="meta-llama/llama-3.1-8b-instruct",
        spec=find_by_name("openrouter"),
        extra_body=extra_body,
        provider_name="openrouter",
    )


def _vllm(extra_body: dict[str, Any] | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="k",
        api_base="http://localhost:8000/v1",
        default_model="Qwen/Qwen3-8B",
        spec=find_by_name("vllm"),
        extra_body=extra_body,
        provider_name="vllm",
    )


def _ctx(sampling: Sampling | None = None, **extras: Any) -> ProviderCallContext:
    return ProviderCallContext(request=RequestExtras(sampling=sampling, **extras))


def _without_messages(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key != "messages"}


# --- openai-compatible ------------------------------------------------------ #


@pytest.mark.parametrize(
    ("make", "configured", "expected_body"),
    [
        (
            _openrouter,
            {"provider": {"sort": "price"}},
            {
                "provider": {"sort": "price", "order": ["x"]},
                "top_k": 40,
                "min_p": 0.05,
                "repetition_penalty": 1.1,
            },
        ),
        (
            _vllm,
            {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 5},
            {
                "chat_template_kwargs": {"enable_thinking": False},
                # The explicit per-request top_k wins over the configured one.
                "top_k": 40,
                "min_p": 0.05,
                "repetition_penalty": 1.1,
                "provider": {"order": ["x"]},
            },
        ),
    ],
    ids=["openrouter", "vllm"],
)
async def test_openai_compat_every_field_snapshot(make, configured, expected_body) -> None:
    provider = make(configured)
    calls: list[dict[str, Any]] = []
    provider._client = _openai_client(calls)
    sink = RecordingSink()
    provider.trace_sink = sink

    response = await provider.chat_with_retry(
        MESSAGES,
        provider_context=_ctx(EVERY_FIELD, extra_body={"provider": {"order": ["x"]}}),
    )

    assert response.content == "ok"
    assert len(calls) == 1
    assert _without_messages(calls[0]) == {
        "model": provider.default_model,
        "max_tokens": 256,
        "reasoning_effort": "low",
        "top_p": 0.9,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.2,
        "seed": 7,
        "stop": ["END"],
        "logit_bias": {"50256": -100},
        "extra_body": expected_body,
    }
    # With reasoning_effort set these models reject temperature: reported, not sent.
    assert sink.events == [{
        "event": "sampling.dropped",
        "provider": provider.provider_name,
        "model": provider.default_model,
        "fields": ["temperature"],
    }]


async def test_openai_compat_sampling_overrides_call_args_and_generation() -> None:
    provider = _vllm()
    provider.generation = GenerationSettings(temperature=0.9, max_tokens=999)
    calls: list[dict[str, Any]] = []
    provider._client = _openai_client(calls)

    await provider.chat_with_retry(
        MESSAGES, temperature=0.5, provider_context=_ctx(Sampling(temperature=0.0)),
    )

    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 999  # unset sampling field keeps the preset default


async def test_openai_compat_stream_path_sends_request_extras() -> None:
    provider = _vllm()
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise RuntimeError("stop after recording")

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    await provider.chat_stream_with_retry(
        MESSAGES,
        provider_context=_ctx(
            Sampling(top_k=3, seed=1), extra_body={"a": 1}, response_format={"type": "json_object"},
        ),
    )

    assert calls
    assert calls[0]["seed"] == 1
    assert calls[0]["extra_body"] == {"top_k": 3, "a": 1}
    assert calls[0]["response_format"] == {"type": "json_object"}


async def test_openai_compat_raise_sends_nothing() -> None:
    provider = _openrouter()
    calls: list[dict[str, Any]] = []
    provider._client = _openai_client(calls)

    with pytest.raises(UnsupportedRequestError) as info:
        await provider.chat_with_retry(
            MESSAGES, provider_context=_ctx(EVERY_FIELD, on_unsupported="raise"),
        )

    assert info.value.fields == ("temperature",)
    assert info.value.provider == "openrouter"
    assert calls == []


async def test_responses_api_path_drops_chat_only_fields() -> None:
    provider = OpenAICompatProvider(
        api_key="k", default_model="gpt-5", spec=find_by_name("openai"), provider_name="openai",
    )
    supported = provider._sampling_support("gpt-5", "low")
    assert supported == frozenset({"max_tokens", "reasoning_effort", "top_p", "extra_body"})
    body = provider._build_responses_body(
        MESSAGES, None, "gpt-5", 100, 0.7, "low", None,
        provider_context=_ctx(Sampling(top_p=0.5, max_tokens=64), extra_body={"user": "u"}),
    )
    assert body["top_p"] == 0.5
    assert body["max_output_tokens"] == 64
    assert body["user"] == "u"


# --- anthropic -------------------------------------------------------------- #


def _anthropic() -> tuple[AnthropicProvider, list[dict[str, Any]]]:
    provider = AnthropicProvider(api_key="k", default_model="claude-sonnet-4-5")
    calls: list[dict[str, Any]] = []
    provider._client = _anthropic_client(calls)
    return provider, calls


async def test_anthropic_supported_subset_snapshot() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink
    sampling = Sampling(
        temperature=0.2, top_p=0.9, top_k=40, stop=("END", "STOP"), max_tokens=256,
        presence_penalty=0.1, seed=7,
    )

    await provider.chat_with_retry(
        MESSAGES, provider_context=_ctx(sampling, extra_body={"metadata": {"user_id": "u"}}),
    )

    # Claude 4.x rejects temperature together with top_p: temperature wins.
    assert _without_messages(calls[0]) == {
        "model": "claude-sonnet-4-5",
        "max_tokens": 256,
        "temperature": 0.2,
        "top_k": 40,
        "stop_sequences": ["END", "STOP"],
        "extra_body": {"metadata": {"user_id": "u"}},
    }
    assert sink.events == [{
        "event": "sampling.dropped",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "fields": ["top_p", "presence_penalty", "seed"],
    }]


async def test_anthropic_every_field_with_thinking_snapshot() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(MESSAGES, provider_context=_ctx(EVERY_FIELD))

    # Thinking pins temperature, rejects top_k and needs top_p >= 0.95 (0.9 here).
    assert _without_messages(calls[0]) == {
        "model": "claude-sonnet-4-5",
        "max_tokens": 5120,  # budget (1024) + 4096 headroom
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "temperature": 1.0,
        "stop_sequences": ["END"],
    }
    assert sink.events[0]["fields"] == [
        "temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty",
        "repetition_penalty", "logit_bias", "seed",
    ]


async def test_anthropic_top_p_alone_omits_default_temperature() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(MESSAGES, provider_context=_ctx(Sampling(top_p=0.8)))

    assert calls[0]["top_p"] == 0.8
    assert "temperature" not in calls[0]
    assert sink.events == []


async def test_anthropic_temperature_and_top_p_raise() -> None:
    provider, calls = _anthropic()
    with pytest.raises(UnsupportedRequestError) as info:
        await provider.chat_with_retry(
            MESSAGES,
            provider_context=_ctx(Sampling(temperature=0.3, top_p=0.8), on_unsupported="raise"),
        )
    assert info.value.fields == ("top_p",)
    assert calls == []


async def test_anthropic_thinking_accepts_high_top_p_without_temperature() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(
        MESSAGES, reasoning_effort="low", provider_context=_ctx(Sampling(top_p=0.97)),
    )

    sent = calls[0]
    assert sent["thinking"]["type"] == "enabled"
    assert sent["top_p"] == 0.97
    assert "temperature" not in sent
    assert sink.events == []


async def test_anthropic_thinking_low_top_p_dropped_or_raised() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(
        MESSAGES, reasoning_effort="low", provider_context=_ctx(Sampling(top_p=0.9)),
    )
    assert "top_p" not in calls[0]
    assert sink.events[0]["fields"] == ["top_p"]

    with pytest.raises(UnsupportedRequestError) as info:
        await provider.chat_with_retry(
            MESSAGES, reasoning_effort="low",
            provider_context=_ctx(Sampling(top_p=0.9), on_unsupported="raise"),
        )
    assert info.value.fields == ("top_p",)
    assert len(calls) == 1


async def test_anthropic_thinking_drops_temperature_and_top_k() -> None:
    provider, calls = _anthropic()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(
        MESSAGES, reasoning_effort="low",
        provider_context=_ctx(Sampling(temperature=0.2, top_k=10)),
    )

    assert "top_k" not in calls[0]
    assert calls[0]["temperature"] == 1.0  # thinking's own pinned value, not 0.2
    assert sink.events[0]["fields"] == ["temperature", "top_k"]


def test_anthropic_direct_build_kwargs_applies_same_rules() -> None:
    provider, _ = _anthropic()
    both = provider._build_kwargs(
        MESSAGES, None, None, 100, 0.7, None, None,
        provider_context=_ctx(Sampling(temperature=0.3, top_p=0.8)),
    )
    assert both["temperature"] == 0.3 and "top_p" not in both
    thinking = provider._build_kwargs(
        MESSAGES, None, None, 100, 0.7, "low", None,
        provider_context=_ctx(Sampling(top_p=0.5, top_k=3)),
    )
    assert "top_p" not in thinking and "top_k" not in thinking


async def test_anthropic_raise_sends_nothing() -> None:
    provider, calls = _anthropic()
    with pytest.raises(UnsupportedRequestError) as info:
        await provider.chat_with_retry(
            MESSAGES, provider_context=_ctx(Sampling(seed=1), on_unsupported="raise"),
        )
    assert info.value.fields == ("seed",)
    assert calls == []


# --- other providers / wrappers ---------------------------------------------- #


class _PlainProvider(LLMProvider):
    """A provider with only the core chat() arguments (like Bedrock/Azure)."""

    def __init__(self) -> None:
        super().__init__(provider_name="plain")
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "plain-model"


async def test_other_providers_keep_core_fields_and_drop_the_rest() -> None:
    provider = _PlainProvider()
    sink = RecordingSink()
    provider.trace_sink = sink

    await provider.chat_with_retry(
        MESSAGES,
        provider_context=_ctx(
            Sampling(temperature=0.1, max_tokens=12, top_p=0.5), extra_body={"x": 1},
        ),
    )

    assert provider.calls[0]["temperature"] == 0.1
    assert provider.calls[0]["max_tokens"] == 12
    assert sink.events[0]["fields"] == ["top_p", "extra_body"]


async def test_drop_without_trace_sink_only_logs() -> None:
    provider = _PlainProvider()
    response = await provider.chat_with_retry(
        MESSAGES, provider_context=_ctx(Sampling(seed=3)),
    )
    assert response.content == "ok"


async def test_factory_wires_env_trace_to_provider(tmp_path) -> None:
    from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
    from nanobot.providers.factory import make_provider

    sink = RecordingSink()
    env = Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"key": "secret"},
        providers=[ProviderSpec(name="vllm", api_base="http://localhost:8000/v1",
                                credential="key")],
        models=[ModelSpec(name="main", model="Qwen/Qwen3-8B", provider="vllm")],
        default_model="main",
        trace=sink,
    )
    provider = make_provider(env.config, env=env.core)
    calls: list[dict[str, Any]] = []
    inner = provider._primary if isinstance(provider, FallbackProvider) else provider
    inner._client = _openai_client(calls)  # type: ignore[attr-defined]

    await provider.chat_with_retry(
        MESSAGES,
        provider_context=_ctx(Sampling(top_k=20), extra_body={"a": 1}),
    )
    assert calls[0]["extra_body"]["top_k"] == 20
    assert calls[0]["extra_body"]["a"] == 1

    def dropped() -> list[dict[str, Any]]:
        return [e for e in sink.events if e["event"] == "sampling.dropped"]

    assert dropped() == []

    await provider.chat_with_retry(
        MESSAGES, reasoning_effort="high", provider_context=_ctx(Sampling(temperature=0.3)),
    )
    assert dropped() == [{
        "event": "sampling.dropped",
        "provider": "vllm",
        "model": "Qwen/Qwen3-8B",
        "fields": ["temperature"],
    }]


async def test_fallback_wrapper_decides_against_primary_before_sending() -> None:
    primary = _PlainProvider()
    wrapper = FallbackProvider(primary, [], provider_factory=lambda preset: primary)
    sink = RecordingSink()
    wrapper.trace_sink = sink
    assert primary.trace_sink is sink

    with pytest.raises(UnsupportedRequestError):
        await wrapper.chat_with_retry(
            MESSAGES, provider_context=_ctx(Sampling(top_k=1), on_unsupported="raise"),
        )
    assert primary.calls == []

    await wrapper.chat_with_retry(MESSAGES, provider_context=_ctx(Sampling(top_k=1)))
    assert len(sink.events) == 1  # reported once, not again by the primary
