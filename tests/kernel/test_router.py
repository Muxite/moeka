"""Task 16: the router, ``model.dispatch`` and baseline comparators (P3, I6).

Sections:
- Ruling J prerequisites: ledger pricing keyed by ``(provider, model)`` (a shared
  model string under two providers never borrows the other's price or tier) and a
  ``usage_source`` marker separating provider-billed usage from local estimates.
- The cascade: solver -> fast tier -> ``verify`` -> escalate one tier on a recorded
  verification failure.
- The ``model.dispatch`` gate: a dispatch above the slot's ceiling without a
  recorded verification failure goes through ``gate_call``; a justified escalation
  skips the policy but is still audited.
- ``cost_ratio`` against a declared baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.ledger import LedgerObserver, ModelPricing, PricingTable
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.providers.base import LLMResponse, LLMUsage
from nanobot.providers.factory import _ledger_pricing, make_provider


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _env(tmp_path: Path, sink: _RecordingSink) -> CoreEnvironment:
    return CoreEnvironment(
        config=type("_Empty", (), {"section": lambda self, name: {}})(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink,
    )


def _record(provider: str, model: str, usage: LLMUsage | None) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=1,
        duration_ms=10,
        provider=provider,
        model=model,
        source="system",
        stream=False,
        finish_reason="stop",
        usage=usage,
    )


# The exact Ruling J collision: one model string, two providers, two tiers, two prices.
_SHARED = "shared/model-x"


def _collision_config(tmp_path: Path, *, active: str = "cheap") -> Config:
    return Config.model_validate({
        "providers": {
            "openai": {"apiKey": "sk-openai-test"},
            "openrouter": {"apiKey": "sk-or-test"},
        },
        "modelPresets": {
            "cheap": {
                "provider": "openai", "model": _SHARED, "tier": "fast",
                "priceInPerMtok": 1.0, "priceOutPerMtok": 1.0,
            },
            "dear": {
                "provider": "openrouter", "model": _SHARED, "tier": "frontier",
                "priceInPerMtok": 10.0, "priceOutPerMtok": 30.0,
            },
        },
        "agents": {"defaults": {
            "workspace": str(tmp_path / "ws"),
            "modelPreset": active,
            "fallbackModels": ["dear" if active == "cheap" else "cheap"],
        }},
    })


# -- Ruling J (1): pricing keyed by (provider, model) ------------------------------


def test_pricing_table_keys_by_provider_and_model() -> None:
    fast = ModelPricing(tier="fast", price_in=1.0, price_out=1.0)
    frontier = ModelPricing(tier="frontier", price_in=10.0, price_out=30.0)
    table = PricingTable({("openai", _SHARED): fast, ("openrouter", _SHARED): frontier})
    assert table.lookup("openai", _SHARED) == fast
    assert table.lookup("openrouter", _SHARED) == frontier


def test_pricing_table_never_borrows_another_providers_price() -> None:
    table = PricingTable({("openai", _SHARED): ModelPricing(tier="fast", price_in=1, price_out=1)})
    assert table.lookup("anthropic", _SHARED) is None


def test_pricing_table_model_only_key_is_a_wildcard() -> None:
    pricing = ModelPricing(tier="fast", price_in=0.25, price_out=2.0)
    table = PricingTable({"gpt-5-mini": pricing})
    assert table.lookup("openai", "gpt-5-mini") == pricing
    assert table.lookup("azure_openai", "gpt-5-mini") == pricing


def test_pricing_table_conflicting_wildcards_refuse_to_guess() -> None:
    table = PricingTable()
    table.add("m", ModelPricing(tier="fast", price_in=1, price_out=1))
    table.add("m", ModelPricing(tier="frontier", price_in=9, price_out=9))
    assert table.lookup("any", "m") is None
    assert table.collisions and table.collisions[0][0] == "m"


def test_pricing_table_same_key_collision_keeps_first_and_is_detected() -> None:
    first = ModelPricing(tier="fast", price_in=1, price_out=1)
    table = PricingTable()
    table.add(("openai", "m"), first)
    table.add(("openai", "m"), ModelPricing(tier="standard", price_in=2, price_out=2))
    assert table.lookup("openai", "m") == first
    assert table.collisions == [(("openai", "m"), first, ModelPricing("standard", 2, 2))]
    # An identical duplicate is not a collision.
    table.add(("openai", "m"), first)
    assert len(table.collisions) == 1


def test_factory_pricing_resolves_each_presets_provider(tmp_path: Path) -> None:
    config = _collision_config(tmp_path)
    resolved = config.model_presets["cheap"]
    table = _ledger_pricing(config, resolved, [config.model_presets["dear"]])
    assert table.lookup("openai", _SHARED).tier == "fast"
    assert table.lookup("openrouter", _SHARED).tier == "frontier"


def test_regression_shared_model_string_no_longer_misattributes(tmp_path: Path) -> None:
    """The OLD bug: pricing keyed on model name only, first preset wins.

    Primary ``cheap`` (openai, fast, $1) and fallback ``dear`` (openrouter, frontier,
    $10/$30) share one model string. A call physically served by openrouter used to
    be recorded as fast tier at the openai price. Each provider's call must now get
    its own tier and price.
    """
    config = _collision_config(tmp_path)
    observer = LedgerObserver(
        pricing=_ledger_pricing(config, config.model_presets["cheap"],
                                [config.model_presets["dear"]]),
    )
    usage = LLMUsage.reported(input_tokens=1_000_000, output_tokens=1_000_000)

    via_openrouter = observer.event_for(_record("openrouter", _SHARED, usage))
    via_openai = observer.event_for(_record("openai", _SHARED, usage))

    assert via_openrouter.tier == "frontier"
    assert via_openrouter.cost_usd == pytest.approx(40.0)
    assert via_openai.tier == "fast"
    assert via_openai.cost_usd == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_regression_real_failover_is_priced_at_the_fallback(tmp_path: Path) -> None:
    """End to end through make_provider: primary errors, fallback serves the call."""
    sink = _RecordingSink()
    provider = make_provider(_collision_config(tmp_path), env=_env(tmp_path, sink))
    assert type(provider).__name__ == "FallbackProvider"

    async def _primary_fails(**_kwargs: object) -> LLMResponse:
        return LLMResponse(
            content=None, finish_reason="error", error_kind="auth",
            usage=LLMUsage.reported(input_tokens=0, output_tokens=0),
        )

    async def _fallback_ok(**_kwargs: object) -> LLMResponse:
        return LLMResponse(
            content="ok",
            usage=LLMUsage.reported(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    provider._primary.chat = _primary_fails
    real_factory = provider._provider_factory

    def _factory(preset: Any) -> Any:
        leaf = real_factory(preset)
        leaf.chat = _fallback_ok
        return leaf

    provider._provider_factory = _factory

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model=_SHARED,
    )

    assert response.content == "ok"
    ledger = [e for e in sink.events if e.get("event") == "model.call"]
    served = [e for e in ledger if e["provider"] == "openrouter"]
    assert len(served) == 1
    assert served[0]["tier"] == "frontier"
    assert served[0]["cost_usd"] == pytest.approx(40.0)
    for event in ledger:
        if event["provider"] == "openai":
            assert event["tier"] == "fast"


# -- Ruling J (2): usage_source separates billed from estimated usage -------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (LLMUsage.reported(input_tokens=10, output_tokens=5), "reported"),
        (LLMUsage.estimated(input_tokens=10, output_tokens=5), "estimated"),
        (None, "none"),
    ],
)
def test_ledger_event_carries_usage_source(usage: LLMUsage | None, expected: str) -> None:
    observer = LedgerObserver(pricing={"m": ModelPricing(tier="fast", price_in=1, price_out=1)})
    record = _record("openai", "m", usage)
    event = observer.event_for(record)
    assert record.usage_source == expected
    assert event.usage_source == expected
    assert event.to_trace()["usage_source"] == expected


def test_estimated_cost_is_not_billed() -> None:
    observer = LedgerObserver(pricing={"m": ModelPricing(tier="fast", price_in=1, price_out=1)})
    estimated = observer.event_for(
        _record("openai", "m", LLMUsage.estimated(input_tokens=10, output_tokens=5)),
    )
    reported = observer.event_for(
        _record("openai", "m", LLMUsage.reported(input_tokens=10, output_tokens=5)),
    )
    # The estimate still carries a number (useful as a hint), but is not ground truth.
    assert estimated.cost_usd is not None
    assert estimated.cost_is_billed is False
    assert reported.cost_is_billed is True


@pytest.mark.asyncio
async def test_provider_without_usage_is_flagged_estimated_end_to_end(tmp_path: Path) -> None:
    sink = _RecordingSink()
    config = Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-ledger-test"}},
        "modelPresets": {"work": {
            "provider": "openai", "model": "gpt-5-mini", "tier": "fast",
            "priceInPerMtok": 1.0, "priceOutPerMtok": 1.0,
        }},
        "agents": {"defaults": {"workspace": str(tmp_path / "ws"), "modelPreset": "work"}},
    })
    provider = make_provider(config, env=_env(tmp_path, sink))

    async def _no_usage(**_kwargs: object) -> LLMResponse:
        return LLMResponse(content="ok")  # the provider reported no usage at all

    provider.chat = _no_usage
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="gpt-5-mini")

    ledger = [e for e in sink.events if e.get("event") == "model.call"]
    assert len(ledger) == 1
    assert ledger[0]["usage_source"] == "estimated"
    assert ledger[0]["tokens_in"] > 0
