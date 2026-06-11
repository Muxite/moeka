"""Tests for acomplete_stream / complete_stream — streaming one-shot completion."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.api.complete import acomplete_stream, complete_stream

_CONFIG = {"providers": {"openrouter": {"apiKey": "sk-test"}}}


class _StreamingStubProvider:
    """Streams a reply in fixed chunks via on_content_delta."""

    def __init__(self, chunks: list[str], finish_reason: str = "stop"):
        self._chunks = list(chunks)
        self._finish_reason = finish_reason
        self.calls: list[list[dict]] = []

    async def chat_stream_with_retry(
        self, *, messages, max_tokens=None, temperature=None, on_content_delta=None,
    ):
        self.calls.append(messages)
        for chunk in self._chunks:
            if on_content_delta:
                await on_content_delta(chunk)
        return SimpleNamespace(
            content="".join(self._chunks),
            finish_reason=self._finish_reason,
            error_type=None,
        )


def _install_stub(monkeypatch, stub) -> None:
    import nanobot.providers.factory as factory

    monkeypatch.setattr(
        factory,
        "make_provider",
        lambda config, *, preset_name=None, preset=None, model=None: stub,
    )


async def test_acomplete_stream_yields_chunks(monkeypatch):
    stub = _StreamingStubProvider(["hel", "lo ", "world"])
    _install_stub(monkeypatch, stub)
    chunks = [c async for c in acomplete_stream("hi", config_dict=dict(_CONFIG))]
    assert chunks == ["hel", "lo ", "world"]


async def test_acomplete_stream_system_prompt_in_messages(monkeypatch):
    stub = _StreamingStubProvider(["x"])
    _install_stub(monkeypatch, stub)
    async for _ in acomplete_stream("hi", system="be terse", config_dict=dict(_CONFIG)):
        pass
    assert stub.calls[0][0] == {"role": "system", "content": "be terse"}


async def test_acomplete_stream_error_raises(monkeypatch):
    stub = _StreamingStubProvider(["partial"], finish_reason="error")
    _install_stub(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="streaming completion failed"):
        async for _ in acomplete_stream("hi", config_dict=dict(_CONFIG)):
            pass


def test_complete_stream_sync_bridge(monkeypatch):
    stub = _StreamingStubProvider(["a", "b", "c"])
    _install_stub(monkeypatch, stub)
    assert list(complete_stream("hi", config_dict=dict(_CONFIG))) == ["a", "b", "c"]


def test_complete_stream_propagates_errors(monkeypatch):
    stub = _StreamingStubProvider(["x"], finish_reason="error")
    _install_stub(monkeypatch, stub)
    with pytest.raises(RuntimeError):
        list(complete_stream("hi", config_dict=dict(_CONFIG)))


async def test_complete_stream_rejects_running_loop(monkeypatch):
    stub = _StreamingStubProvider(["x"])
    _install_stub(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="acomplete_stream"):
        next(complete_stream("hi", config_dict=dict(_CONFIG)))
