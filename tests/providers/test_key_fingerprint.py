"""Spec 009: providers fingerprint the API key they send, and call records carry it."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.ledger import LedgerObserver
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage, key_fingerprint
from nanobot.providers.factory import _ledger_pricing
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

# Fake keys only: never a real key in a test.
KEY_A = "sk-or-v1-aaaaFAKEaaaa0000111122223333444455556666"
KEY_B = "sk-or-v1-bbbbFAKEbbbb7777888899990000aaaabbbbcccc"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def _expected(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:12]


class _OneShotProvider(LLMProvider):
    _CHAT_RETRY_DELAYS = ()

    def __init__(self, api_key: str | None) -> None:
        super().__init__(api_key=api_key, provider_name="openrouter")

    async def chat(self, **_kwargs: object) -> LLMResponse:
        return LLMResponse(
            content="ok", usage=LLMUsage.reported(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    def get_default_model(self) -> str:
        return "openai/gpt-6-luna"


def test_key_fp_matches_the_terms_definition() -> None:
    provider = OpenAICompatProvider(api_key=KEY_A, provider_name="openrouter")
    assert provider.key_fp == _expected(KEY_A)
    assert re.fullmatch(r"[0-9a-f]{12}", provider.key_fp)


def test_surrounding_whitespace_is_ignored() -> None:
    assert key_fingerprint(f"  {KEY_A}\n") == _expected(KEY_A)


@pytest.mark.parametrize("key", [None, "", "   ", "\n\t"])
def test_no_key_has_no_fingerprint(key: str | None) -> None:
    assert OpenAICompatProvider(api_key=key, provider_name="openrouter").key_fp is None


def test_key_fp_follows_a_changed_api_key() -> None:
    provider = OpenAICompatProvider(api_key=KEY_A, provider_name="openrouter")
    provider.api_key = KEY_B
    assert provider.key_fp == _expected(KEY_B)
    provider.api_key = None
    assert provider.key_fp is None


def test_key_fp_is_read_only() -> None:
    provider = OpenAICompatProvider(api_key=KEY_A, provider_name="openrouter")
    with pytest.raises(AttributeError):
        provider.key_fp = "000000000000"  # type: ignore[misc]


async def test_observed_call_records_carry_each_providers_fingerprint() -> None:
    records: list[LLMCallRecord] = []
    for key in (KEY_A, KEY_B, None):
        provider = _OneShotProvider(key)
        provider.set_llm_call_observer(records.append)
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    assert [r.key_fp for r in records] == [_expected(KEY_A), _expected(KEY_B), None]


def _record(**extra: object) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=1, duration_ms=1, provider="openrouter", model="m", source="api",
        stream=False, finish_reason="stop", **extra,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "bad", ["ABCDEF012345", "abc", "0123456789abc", "g23456789abc", KEY_A, 123456789012],
)
def test_record_rejects_a_malformed_key_fp_without_echoing_it(bad: object) -> None:
    with pytest.raises(ValueError) as info:
        _record(key_fp=bad)
    assert str(bad) not in str(info.value)


def test_record_repr_shows_the_fingerprint_never_the_key() -> None:
    record = _record(key_fp=_expected(KEY_A))
    assert _expected(KEY_A) in repr(record)
    assert KEY_A[:8] not in repr(record) and KEY_A[-8:] not in str(record)
    assert _record().key_fp is None


async def test_ledger_event_carries_key_fp_and_never_the_key() -> None:
    events: list[dict] = []

    class _Sink:
        def emit(self, event: dict) -> None:
            events.append(event)

    provider = _OneShotProvider(KEY_A)
    provider.set_llm_call_observer(LedgerObserver(sink=_Sink()))
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    [event] = events
    assert event["key_fp"] == _expected(KEY_A)
    dumped = json.dumps(event, default=str)
    for i in range(len(KEY_A) - 7):
        assert KEY_A[i:i + 8] not in dumped


# -- FR-007: the gpt-6-luna preset --------------------------------------------------------

LUNA = {
    "model": "openai/gpt-6-luna", "provider": "openrouter", "tier": "fast",
    "price_in_per_mtok": 0.10, "price_out_per_mtok": 0.50, "context_window_tokens": 1000000,
}


def test_luna_preset_prices_one_million_each_way_at_sixty_cents(tmp_path: Path) -> None:
    config = Config.model_validate({
        "providers": {"openrouter": {"apiKey": KEY_A}},
        "modelPresets": {"luna": LUNA},
        "agents": {"defaults": {"workspace": str(tmp_path / "ws"), "modelPreset": "luna"}},
    })
    preset = config.model_presets["luna"]
    assert preset.context_window_tokens == 1_000_000
    observer = LedgerObserver(pricing=_ledger_pricing(config, preset, []))
    record = replace(
        _record(usage=LLMUsage.reported(input_tokens=1_000_000, output_tokens=1_000_000)),
        model="openai/gpt-6-luna",
    )
    event = observer.event_for(record)
    assert event.tier == "fast"
    assert event.cost_usd == pytest.approx(0.60, abs=1e-12)


def test_docs_carry_the_luna_preset_and_key_matrix() -> None:
    text = DOCS.read_text()
    assert "\n## Model presets: gpt-6-luna\n" in text
    section = text.split("\n## Model presets: gpt-6-luna\n", 1)[1].split("\n## ", 1)[0]
    blob = re.search(r'\{"model": "openai/gpt-6-luna".*?\}', section)
    assert blob is not None and json.loads(blob.group(0)) == LUNA
    assert "~/projects/moeka/keys.env" in section
    assert "~/projects/awork/keys.env" in section
    assert section.count("OPENROUTER_API_KEY") >= 2
