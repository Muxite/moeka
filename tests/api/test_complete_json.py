"""Tests for acomplete_json — structured (JSON-constrained) one-shot completion.

Stubbed provider; covers fence-stripping, the parse-retry loop with error
feedback, JSON Schema instruction injection, and pydantic validation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from nanobot.api.complete import _extract_json_text, acomplete_json

_CONFIG = {"providers": {"openrouter": {"apiKey": "sk-test"}}}


class _StubProvider:
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
        self.calls.append(messages)
        return SimpleNamespace(
            content=self._replies.pop(0), finish_reason="stop", error_type=None
        )


def _install_stub(monkeypatch, replies: list[str]) -> _StubProvider:
    stub = _StubProvider(replies)
    import nanobot.providers.factory as factory

    monkeypatch.setattr(
        factory,
        "make_provider",
        lambda config, *, preset_name=None, preset=None, model=None: stub,
    )
    return stub


# ---------------------------------------------------------------------------
# _extract_json_text
# ---------------------------------------------------------------------------

def test_extract_plain_json():
    assert _extract_json_text('{"a": 1}') == '{"a": 1}'


def test_extract_fenced_json():
    assert _extract_json_text('Here:\n```json\n{"a": 1}\n```\nDone.') == '{"a": 1}'


def test_extract_fenced_without_language():
    assert _extract_json_text('```\n[1, 2]\n```') == "[1, 2]"


def test_extract_embedded_object():
    assert _extract_json_text('Sure! {"a": {"b": 2}} hope that helps') == '{"a": {"b": 2}}'


def test_extract_embedded_array():
    assert _extract_json_text("the list is [1, 2, 3].") == "[1, 2, 3]"


# ---------------------------------------------------------------------------
# acomplete_json
# ---------------------------------------------------------------------------

async def test_valid_json_first_try(monkeypatch):
    stub = _install_stub(monkeypatch, ['{"verdict": "accept"}'])
    out = await acomplete_json("judge this", config_dict=dict(_CONFIG))
    assert out == {"verdict": "accept"}
    assert len(stub.calls) == 1
    # The JSON instruction rides in the system prompt.
    assert "ONLY with valid JSON" in stub.calls[0][0]["content"]


async def test_fenced_json_is_accepted(monkeypatch):
    _install_stub(monkeypatch, ['```json\n{"ok": true}\n```'])
    assert await acomplete_json("p", config_dict=dict(_CONFIG)) == {"ok": True}


async def test_invalid_then_valid_retries_with_error_feedback(monkeypatch):
    stub = _install_stub(monkeypatch, ["not json at all", '{"fixed": 1}'])
    out = await acomplete_json("p", config_dict=dict(_CONFIG))
    assert out == {"fixed": 1}
    assert len(stub.calls) == 2
    retry_prompt = stub.calls[1][-1]["content"]
    assert "was not valid" in retry_prompt
    assert "not json at all" in retry_prompt  # the bad reply is fed back


async def test_retries_exhausted_raises(monkeypatch):
    _install_stub(monkeypatch, ["bad", "worse", "still bad"])
    with pytest.raises(ValueError, match="3 attempt"):
        await acomplete_json("p", retries=2, config_dict=dict(_CONFIG))


async def test_schema_injected_into_system(monkeypatch):
    stub = _install_stub(monkeypatch, ["{}"])
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    await acomplete_json("p", schema=schema, config_dict=dict(_CONFIG))
    system = stub.calls[0][0]["content"]
    assert '"score"' in system


async def test_user_system_prompt_is_preserved(monkeypatch):
    stub = _install_stub(monkeypatch, ["{}"])
    await acomplete_json("p", system="you are a recruiter", config_dict=dict(_CONFIG))
    system = stub.calls[0][0]["content"]
    assert system.startswith("you are a recruiter")
    assert "ONLY with valid JSON" in system


class _Verdict(BaseModel):
    verdict: str
    overall: int


async def test_model_cls_returns_validated_instance(monkeypatch):
    _install_stub(monkeypatch, ['{"verdict": "accept", "overall": 8}'])
    out = await acomplete_json("p", model_cls=_Verdict, config_dict=dict(_CONFIG))
    assert isinstance(out, _Verdict)
    assert out.overall == 8


async def test_model_cls_validation_failure_retries(monkeypatch):
    stub = _install_stub(
        monkeypatch,
        ['{"verdict": "accept"}', '{"verdict": "accept", "overall": 7}'],
    )
    out = await acomplete_json("p", model_cls=_Verdict, config_dict=dict(_CONFIG))
    assert out.overall == 7
    assert len(stub.calls) == 2


async def test_model_cls_schema_reaches_model(monkeypatch):
    stub = _install_stub(monkeypatch, ['{"verdict": "a", "overall": 1}'])
    await acomplete_json("p", model_cls=_Verdict, config_dict=dict(_CONFIG))
    assert '"overall"' in stub.calls[0][0]["content"]
