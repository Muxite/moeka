"""Tests for nanobot.api.complete — the one-shot programmatic completion API.

The provider is stubbed at the make_provider seam; nothing here touches the
network. Pins the surface awork depends on (prompt/system/images/model/
max_tokens/temperature) and the sync-wrapper event-loop guard.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.api.complete import acomplete, complete, complete_json

_CONFIG = {"providers": {"openrouter": {"apiKey": "sk-test"}}}


class _StubProvider:
    def __init__(self, replies: list[str | None]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
        self.calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        content = self._replies.pop(0)
        return SimpleNamespace(content=content, finish_reason="stop", error_type=None)


def _install_stub(monkeypatch, replies: list[str | None]) -> _StubProvider:
    stub = _StubProvider(replies)

    def fake_make_provider(config, *, preset_name=None, preset=None, model=None):
        stub.model_arg = model
        stub.preset_arg = preset_name
        return stub

    import nanobot.providers.factory as factory

    monkeypatch.setattr(factory, "make_provider", fake_make_provider)
    return stub


async def test_acomplete_happy_path(monkeypatch):
    stub = _install_stub(monkeypatch, ["hello back"])
    out = await acomplete("hello", config_dict=dict(_CONFIG))
    assert out == "hello back"
    [call] = stub.calls
    assert call["messages"] == [{"role": "user", "content": "hello"}]


async def test_acomplete_system_and_overrides(monkeypatch):
    stub = _install_stub(monkeypatch, ["ok"])
    await acomplete(
        "p",
        system="be terse",
        config_dict=dict(_CONFIG),
        model="some/model",
        max_tokens=64,
        temperature=0.1,
    )
    [call] = stub.calls
    assert call["messages"][0] == {"role": "system", "content": "be terse"}
    assert call["max_tokens"] == 64
    assert call["temperature"] == 0.1
    assert stub.model_arg == "some/model"


async def test_acomplete_images_become_multimodal_content(monkeypatch):
    stub = _install_stub(monkeypatch, ["seen"])
    await acomplete(
        "what is this?",
        images=["https://example.com/cat.png", b"\x89PNG fake bytes"],
        config_dict=dict(_CONFIG),
    )
    [call] = stub.calls
    content = call["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == "https://example.com/cat.png"
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_acomplete_none_content_raises(monkeypatch):
    _install_stub(monkeypatch, [None])
    with pytest.raises(RuntimeError, match="returned no content"):
        await acomplete("p", config_dict=dict(_CONFIG))


def test_complete_sync_happy_path(monkeypatch):
    _install_stub(monkeypatch, ["sync reply"])
    assert complete("p", config_dict=dict(_CONFIG)) == "sync reply"


async def test_complete_refuses_inside_event_loop():
    with pytest.raises(RuntimeError, match="await acomplete"):
        complete("p", config_dict=dict(_CONFIG))


def test_complete_json_refuses_inside_event_loop():
    async def attempt():
        complete_json("p", config_dict=dict(_CONFIG))

    with pytest.raises(RuntimeError, match="await acomplete_json"):
        asyncio.run(attempt())
