"""awork compatibility contract.

awork (backend/awork/llm.py) consumes moeka's embeddable-kernel surface —
``complete``/``acomplete``/``complete_json``/``acomplete_json``,
``MoekaCore.scoped(profile=...)``, and ``nanobot.core.vec.open_vec_store`` —
directly, with exact call shapes. This file exercises the *real* functions
(no monkeypatching of the functions under test) against a fake provider
injected at the provider-construction seam
(``nanobot.providers.factory.make_provider``, per ``tests/api/test_complete*.py``),
so a regression here means awork's submodule bump breaks.

An ``inspect.signature`` assertion pins the exact kwargs awork passes, so any
future rename/removal fails loudly here instead of silently in awork.
"""

from __future__ import annotations

import inspect
import re

import pytest

from nanobot.api.complete import acomplete, acomplete_json, complete, complete_json
from nanobot.config.schema import AgentProfileConfig, Config
from nanobot.core import MoekaCore
from nanobot.providers.base import LLMResponse, LLMUsage

# Task 12: every function/method this file calls directly is now a deprecation
# shim (behaviour unchanged; see nanobot/_deprecation.py) — allow the warning
# here instead of a global ignore. tests/test_deprecation_shims.py pins the
# exact message and call-site for each shim; this file must keep passing
# unchanged apart from that.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_CONFIG_DICT = {"providers": {"openrouter": {"apiKey": "sk-test"}}}


# ---------------------------------------------------------------------------
# Fake provider — injected at the make_provider seam, like tests/api/*.
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Mimics the real provider's chat_with_retry contract closely enough to
    exercise usage_sink and the response_format-carrying ``provider_context``.
    """

    def __init__(self, replies: list[str], *, reject_native: bool = False,
                 cache_read_tokens: int | None = None):
        self._replies = list(replies)
        self.calls: list[dict] = []
        self._reject_native = reject_native
        self._cache_read_tokens = cache_read_tokens

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None,
                               provider_context=None):
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "provider_context": provider_context,
            }
        )
        response_format = None
        if provider_context is not None and provider_context.request is not None:
            response_format = provider_context.request.response_format
        if self._reject_native and response_format is not None:
            # Mirrors OpenAICompatProvider.chat(): provider errors come back as
            # an error LLMResponse, never a raised exception.
            return LLMResponse(content="Error: response_format not supported", finish_reason="error")
        content = self._replies.pop(0)
        usage = LLMUsage.reported(
            input_tokens=11, output_tokens=7, cache_read_tokens=self._cache_read_tokens,
        )
        return LLMResponse(content=content, finish_reason="stop", usage=usage)


def _install(monkeypatch, provider: _FakeProvider) -> list[dict]:
    made: list[dict] = []

    def _make(config, *, preset_name=None, preset=None, model=None, **kw):
        made.append({"preset_name": preset_name, "model": model, **kw})
        return provider

    import nanobot.providers.factory as factory

    monkeypatch.setattr(factory, "make_provider", _make)
    return made


# ---------------------------------------------------------------------------
# complete / acomplete: usage_sink, images, config vs config_dict
# ---------------------------------------------------------------------------


async def test_acomplete_reports_usage_to_sink(monkeypatch):
    """awork's UsageLedger reads legacy OpenAI-shaped keys (prompt_tokens/
    completion_tokens/total_tokens, cached_tokens for cache reads) — see
    backend/awork/llm.py:165-166 (``_cache_tokens``) and :328-329, :599-601
    (event ingestion) — not LLMUsage's canonical field names."""
    provider = _FakeProvider(["hello"])
    _install(monkeypatch, provider)
    seen: list[dict] = []
    out = await acomplete(
        "hi", model="some/model", config_dict=dict(_CONFIG_DICT), usage_sink=seen.append,
    )
    assert out == "hello"
    assert len(seen) == 1
    payload = seen[0]
    assert payload["model"] == "some/model"
    assert payload["prompt_tokens"] == 11
    assert payload["completion_tokens"] == 7
    assert payload["total_tokens"] == 18
    assert "input_tokens" not in payload  # canonical LLMUsage key must not leak
    assert "output_tokens" not in payload


def test_complete_sync_with_usage_sink_and_config_object(monkeypatch):
    provider = _FakeProvider(["sync hello"])
    _install(monkeypatch, provider)
    config = Config.model_validate(dict(_CONFIG_DICT))
    seen: list[dict] = []
    out = complete("hi", config=config, usage_sink=seen.append)
    assert out == "sync hello"
    assert len(seen) == 1
    assert seen[0]["model"] is None
    assert seen[0]["prompt_tokens"] == 11
    assert seen[0]["completion_tokens"] == 7


async def test_acomplete_usage_sink_reports_cache_read_key_awork_recognises(monkeypatch):
    """awork's ``_cache_tokens`` reads ``cached_tokens`` or
    ``cache_read_input_tokens`` for the cache-read count (llm.py:165-166)."""
    provider = _FakeProvider(["hi"], cache_read_tokens=4)
    _install(monkeypatch, provider)
    seen: list[dict] = []
    await acomplete("hi", config_dict=dict(_CONFIG_DICT), usage_sink=seen.append)
    assert seen[0].get("cached_tokens") == 4


async def test_acomplete_images_and_system_awork_shape(monkeypatch):
    provider = _FakeProvider(["seen"])
    _install(monkeypatch, provider)
    out = await acomplete(
        "what is this?",
        system="be terse",
        images=["https://example.com/cat.png"],
        model="m", preset="p", max_tokens=100, temperature=0.2,
        config_dict=dict(_CONFIG_DICT),
    )
    assert out == "seen"
    [call] = provider.calls
    assert call["messages"][0] == {"role": "system", "content": "be terse"}
    content = call["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == "https://example.com/cat.png"


async def test_acomplete_usage_sink_error_never_breaks_completion(monkeypatch):
    provider = _FakeProvider(["ok"])
    _install(monkeypatch, provider)

    def _boom(_usage):
        raise RuntimeError("metering backend down")

    out = await acomplete("hi", config_dict=dict(_CONFIG_DICT), usage_sink=_boom)
    assert out == "ok"


# awork's own sentinel regex (backend/awork/llm.py:1012) for sniffing a
# provider error out of moeka's completion text — copied verbatim so this
# test fails the moment `complete()`'s error-as-content contract breaks,
# instead of only failing inside awork after a submodule bump.
_AWORK_PROVIDER_SENTINEL_RE = re.compile(r"(?:^|['\"])error(?: calling [^:'\"]{0,60})?:")


class _ErrorProvider:
    """A provider whose chat_with_retry returns a provider-error LLMResponse —
    moeka never raises on this, it returns the error as content text so hosts
    can sniff it (see nanobot.api.complete.acomplete's docstring)."""

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
        return LLMResponse(
            content='Error: {"message": "insufficient credits"}', finish_reason="error",
        )


def test_complete_returns_provider_error_text_matching_awork_sentinel(monkeypatch):
    """acomplete()/complete() must not raise on a provider error — awork
    detects it itself via `_PROVIDER_SENTINEL_RE`."""
    def _make(config, *, preset_name=None, preset=None, model=None, **kw):
        return _ErrorProvider()

    import nanobot.providers.factory as factory

    monkeypatch.setattr(factory, "make_provider", _make)

    out = complete("hi", config_dict=dict(_CONFIG_DICT))
    # awork lowercases before matching (llm.py:1022) — replicate that here.
    assert _AWORK_PROVIDER_SENTINEL_RE.search(out.lower())


# ---------------------------------------------------------------------------
# complete_json / acomplete_json: native response_format + fallback, retries
# ---------------------------------------------------------------------------


async def test_acomplete_json_sends_native_response_format_when_schema_given(monkeypatch):
    provider = _FakeProvider(['{"verdict": "accept"}'])
    _install(monkeypatch, provider)
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    out = await acomplete_json("judge this", schema=schema, config_dict=dict(_CONFIG_DICT))
    assert out == {"verdict": "accept"}
    [call] = provider.calls
    ctx = call["provider_context"]
    assert ctx is not None and ctx.request.response_format["type"] == "json_schema"


async def test_acomplete_json_falls_back_when_provider_rejects_native(monkeypatch):
    provider = _FakeProvider(['{"verdict": "accept"}'], reject_native=True)
    _install(monkeypatch, provider)
    schema = {"type": "object"}
    out = await acomplete_json("judge this", schema=schema, retries=2,
                                config_dict=dict(_CONFIG_DICT))
    assert out == {"verdict": "accept"}
    # First attempt carried response_format and was rejected; the retry that
    # actually produced content carried none.
    assert provider.calls[0]["provider_context"].request.response_format is not None
    succeeding = [c for c in provider.calls if c["provider_context"] is None]
    assert succeeding, "fallback attempt must drop response_format"


def test_complete_json_sync_with_usage_sink_and_retries(monkeypatch):
    provider = _FakeProvider(["not json", '{"ok": true}'])
    _install(monkeypatch, provider)
    seen: list[dict] = []
    out = complete_json("q", retries=1, usage_sink=seen.append, config_dict=dict(_CONFIG_DICT))
    assert out == {"ok": True}
    assert len(provider.calls) == 2
    assert len(seen) == 2  # usage reported on both attempts


# ---------------------------------------------------------------------------
# MoekaCore.scoped(profile=AgentProfileConfig(...), config=..., workspace=None)
# ---------------------------------------------------------------------------


def test_moeka_core_scoped_with_agent_profile_config_builds_and_cleans_up():
    config = Config.model_validate(dict(_CONFIG_DICT))
    profile = AgentProfileConfig(
        tools_allow=["web_search"], system_prompt="You are terse.", skills_include=[],
    )
    with MoekaCore.scoped(profile=profile, config=config, workspace=None) as core:
        ws = core.workspace
        assert ws.exists()
        assert set(core.loop.tools.tool_names) <= {"web_search"}
    assert not ws.exists()


# ---------------------------------------------------------------------------
# open_vec_store: the methods awork's research agent uses for vector memory.
# ---------------------------------------------------------------------------


def test_open_vec_store_awork_surface(tmp_path):
    pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")
    pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")
    from nanobot.core.vec import open_vec_store

    store = open_vec_store(tmp_path / "vec.db")
    assert store.available
    assert store.keyword_available
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    added = store.add_documents("some document text about awork research agents", source="doc1")
    assert added >= 0
    assert store.count_documents() >= 0
    results = store.search_documents_scored("research agents")
    assert isinstance(results, list)
    store.clear_documents()
    assert store.count_documents() == 0


# ---------------------------------------------------------------------------
# Signature contract — pins the exact kwargs awork passes.
# ---------------------------------------------------------------------------


def _params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def test_complete_signature_matches_awork_contract():
    expected = {
        "system", "images", "model", "preset", "max_tokens", "temperature",
        "usage_sink", "config", "config_dict",
    }
    assert expected <= _params(complete)
    assert expected <= _params(acomplete)


def test_complete_json_signature_matches_awork_contract():
    expected = {
        "system", "images", "model", "preset", "max_tokens", "temperature",
        "usage_sink", "config", "config_dict", "schema", "retries",
    }
    # complete_json/acomplete_json forward most of these through **kwargs, but
    # schema/retries/system must be explicit — and the rest must be acceptable
    # kwargs on the underlying acomplete they delegate to.
    assert {"schema", "retries", "system"} <= _params(complete_json)
    assert {"schema", "retries", "system"} <= _params(acomplete_json)
    assert expected - {"schema", "retries", "system"} <= _params(acomplete)


def test_prompt_is_positional_for_all_four():
    for fn in (complete, acomplete, complete_json, acomplete_json):
        first = next(iter(inspect.signature(fn).parameters.values()))
        assert first.name == "prompt"
        assert first.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
