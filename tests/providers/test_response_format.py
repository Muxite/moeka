"""Native structured output (response_format) threading + acomplete_json fallback."""

from __future__ import annotations

import sys
from typing import Any

import pytest  # noqa: F401

from nanobot.api.complete import _json_response_format, acomplete_json
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


# --- _build_kwargs injection ----------------------------------------------- #

def test_build_kwargs_omits_response_format_by_default():
    kwargs = _make_provider()._build_kwargs(
        messages=_messages(), tools=None, model=None, max_tokens=50,
        temperature=0.1, reasoning_effort=None, tool_choice=None,
    )
    assert "response_format" not in kwargs


def test_build_kwargs_sets_response_format_when_given():
    rf = {"type": "json_object"}
    kwargs = _make_provider()._build_kwargs(
        messages=_messages(), tools=None, model=None, max_tokens=50,
        temperature=0.1, reasoning_effort=None, tool_choice=None,
        response_format=rf,
    )
    assert kwargs["response_format"] == rf


# --- acomplete_json native-first with fallback ----------------------------- #

async def test_acomplete_json_native_success(monkeypatch):
    """When the native call returns valid JSON, one call, response_format passed."""
    seen = {}

    async def fake_acomplete(prompt, *, system=None, response_format=None, **kw):
        seen["response_format"] = response_format
        seen["calls"] = seen.get("calls", 0) + 1
        return '{"ok": true}'

    monkeypatch.setattr(capi, "acomplete", fake_acomplete)
    out = await acomplete_json("q", schema={"type": "object"})
    assert out == {"ok": True}
    assert seen["calls"] == 1
    assert seen["response_format"]["type"] == "json_schema"   # native was used


async def test_acomplete_json_falls_back_when_provider_rejects(monkeypatch):
    """Provider rejects response_format on the first call → drop it and retry."""
    calls = []

    async def fake_acomplete(prompt, *, system=None, response_format=None, **kw):
        calls.append(response_format)
        if response_format is not None:
            raise RuntimeError("400 response_format not supported")
        return '{"ok": 1}'

    monkeypatch.setattr(capi, "acomplete", fake_acomplete)
    out = await acomplete_json("q", schema={"type": "object"}, retries=2)
    assert out == {"ok": 1}
    assert calls[0] is not None and calls[1] is None   # native then plain fallback


async def test_acomplete_json_no_schema_uses_plain_path(monkeypatch):
    """No schema → no native response_format, existing reprompt path unchanged."""
    seen = {}

    async def fake_acomplete(prompt, *, system=None, response_format=None, **kw):
        seen["response_format"] = response_format
        return "[]"

    monkeypatch.setattr(capi, "acomplete", fake_acomplete)
    out = await acomplete_json("q")
    assert out == []
    assert seen["response_format"] is None
