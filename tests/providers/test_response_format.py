"""Native structured output (response_format) threading + acomplete_json fallback."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest  # noqa: F401

from nanobot.api.complete import _json_response_format, acomplete_json
from nanobot.providers.base import ProviderCallContext, RequestExtras
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

# The `nanobot.api` package re-exports the `complete` function, shadowing the
# `nanobot.api.complete` submodule for attribute access — resolve the real module
# object via sys.modules so we can monkeypatch its `acomplete` global.
capi = sys.modules[acomplete_json.__module__]


def _make_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(api_key="k", default_model="test-model")


def _messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hi"}]


# --- _json_response_format ------------------------------------------------- #

def test_response_format_from_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    rf = _json_response_format(schema)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == schema
    assert rf["json_schema"]["strict"] is False


def test_response_format_none_without_schema():
    assert _json_response_format(None) is None
    assert _json_response_format({}) is None


# --- _build_kwargs injection via ProviderCallContext ------------------------ #

def test_build_kwargs_omits_response_format_by_default():
    kwargs = _make_provider()._build_kwargs(
        messages=_messages(), tools=None, model=None, max_tokens=50,
        temperature=0.1, reasoning_effort=None, tool_choice=None,
    )
    assert "response_format" not in kwargs


def test_build_kwargs_omits_response_format_without_request_extras():
    kwargs = _make_provider()._build_kwargs(
        messages=_messages(), tools=None, model=None, max_tokens=50,
        temperature=0.1, reasoning_effort=None, tool_choice=None,
        provider_context=ProviderCallContext(),
    )
    assert "response_format" not in kwargs


def test_build_kwargs_sets_response_format_from_provider_context():
    rf = {"type": "json_object"}
    kwargs = _make_provider()._build_kwargs(
        messages=_messages(), tools=None, model=None, max_tokens=50,
        temperature=0.1, reasoning_effort=None, tool_choice=None,
        provider_context=ProviderCallContext(request=RequestExtras(response_format=rf)),
    )
    assert kwargs["response_format"] == rf


# --- acomplete_json native-first with fallback ----------------------------- #

def _resp(content, finish_reason="stop"):
    return SimpleNamespace(content=content, finish_reason=finish_reason, error_type=None)


async def test_acomplete_json_native_success(monkeypatch):
    """When the native call returns valid JSON, one call, response_format passed.

    acomplete_json calls the response-returning ``_acomplete_response`` helper
    (not ``acomplete``) so it can see ``finish_reason`` — a provider rejection
    of response_format comes back as an error LLMResponse, not a raised
    exception (moeka's `chat()` never lets a provider error propagate).
    """
    seen = {}

    async def fake_response(prompt, *, system=None, response_format=None, **kw):
        seen["response_format"] = response_format
        seen["calls"] = seen.get("calls", 0) + 1
        return _resp('{"ok": true}')

    monkeypatch.setattr(capi, "_acomplete_response", fake_response)
    out = await acomplete_json("q", schema={"type": "object"})
    assert out == {"ok": True}
    assert seen["calls"] == 1
    assert seen["response_format"]["type"] == "json_schema"   # native was used


async def test_acomplete_json_falls_back_when_provider_rejects(monkeypatch):
    """Provider rejects response_format on the first call → drop it and retry."""
    calls = []

    async def fake_response(prompt, *, system=None, response_format=None, **kw):
        calls.append(response_format)
        if response_format is not None:
            return _resp("Error: response_format not supported", finish_reason="error")
        return _resp('{"ok": 1}')

    monkeypatch.setattr(capi, "_acomplete_response", fake_response)
    out = await acomplete_json("q", schema={"type": "object"}, retries=2)
    assert out == {"ok": 1}
    assert calls[0] is not None and calls[1] is None   # native then plain fallback


async def test_acomplete_json_no_schema_uses_plain_path(monkeypatch):
    """No schema → no native response_format, existing reprompt path unchanged."""
    seen = {}

    async def fake_response(prompt, *, system=None, response_format=None, **kw):
        seen["response_format"] = response_format
        return _resp("[]")

    monkeypatch.setattr(capi, "_acomplete_response", fake_response)
    out = await acomplete_json("q")
    assert out == []
    assert seen["response_format"] is None


# --- acomplete threads response_format into a ProviderCallContext ---------- #

async def test_acomplete_builds_provider_context_from_response_format(monkeypatch):
    seen = {}

    class _StubProvider:
        async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None,
                                   provider_context=None):
            seen["provider_context"] = provider_context
            return SimpleNamespace(content="ok", finish_reason="stop", error_type=None)

    import nanobot.providers.factory as factory

    monkeypatch.setattr(
        factory, "make_provider",
        lambda config, *, preset_name=None, preset=None, model=None, **kw: _StubProvider(),
    )

    rf = {"type": "json_object"}
    out = await capi.acomplete(
        "hi", response_format=rf, config_dict={"providers": {"openrouter": {"apiKey": "sk-test"}}},
    )
    assert out == "ok"
    ctx = seen["provider_context"]
    assert isinstance(ctx, ProviderCallContext)
    assert ctx.request.response_format == rf


async def test_acomplete_omits_provider_context_without_response_format(monkeypatch):
    seen = {}

    class _StubProvider:
        async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
            seen["called"] = True
            return SimpleNamespace(content="ok", finish_reason="stop", error_type=None)

    import nanobot.providers.factory as factory

    monkeypatch.setattr(
        factory, "make_provider",
        lambda config, *, preset_name=None, preset=None, model=None, **kw: _StubProvider(),
    )

    out = await capi.acomplete(
        "hi", config_dict={"providers": {"openrouter": {"apiKey": "sk-test"}}},
    )
    assert out == "ok"
    assert seen["called"] is True
