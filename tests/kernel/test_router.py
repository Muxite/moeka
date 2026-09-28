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

import importlib
import json
import math
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from nanobot.config.schema import Config, ModelTier
from nanobot.core import MoekaCore
from nanobot.kernel import baselines as baselines_mod
from nanobot.kernel import router as router_mod
from nanobot.kernel import solvers as solvers_mod
from nanobot.kernel.baselines import Baseline, BaselineRegistry, compare_cost, cost_ratio
from nanobot.kernel.deferred import deferred_log_path
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.ledger import LedgerEvent, LedgerObserver, ModelPricing, PricingTable
from nanobot.kernel.policy import POLICY_MARKER, Allow, DefaultPolicy
from nanobot.kernel.router import (
    MODEL_DISPATCH,
    TIER_ORDER,
    ModelDispatchDeniedError,
    RouteVerificationError,
    TierLadder,
    route,
)
from nanobot.kernel.solvers import Solved, SolverRegistry
from nanobot.llm_usage.context import current_llm_usage_slot
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.providers.base import LLMResponse, LLMUsage
from nanobot.providers.factory import _ledger_pricing, make_provider

# Task 12: no module-wide ignore — only test_acomplete_without_env_keeps_the_old_
# make_provider_call below calls a real (unmocked) public deprecated function and
# is marked individually. Every ``route(...)``/``MoekaCore.think_structured(slot=...)``
# call in this file goes through the router's *unwarned* internal impl (ruling 3)
# and must stay silent — see test_default_dispatch_does_not_warn_kernel_internal.


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


# ================================================================================
# The router cascade
# ================================================================================

complete_mod = importlib.import_module("nanobot.api.complete")


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    registry = SolverRegistry()
    monkeypatch.setattr(solvers_mod, "_DEFAULT_REGISTRY", registry)
    return registry


def _ladder_config(*, ceiling: str | None = None, start: str | None = None,
                   slot: str = "extract.title") -> Config:
    slots = {}
    if ceiling is not None or start is not None:
        slots[slot] = {k: v for k, v in (("ceiling", ceiling), ("start", start)) if v}
    return Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-router-test"}},
        "modelPresets": {
            "tiny": {"provider": "openai", "model": "m-local", "tier": "local"},
            "quick": {"provider": "openai", "model": "m-fast", "tier": "fast"},
            "solid": {"provider": "openai", "model": "m-standard", "tier": "standard"},
            "big": {"provider": "openai", "model": "m-frontier", "tier": "frontier"},
        },
        "router": {"slots": slots},
    })


class _Dispatcher:
    """A fake dispatch: records each (tier, preset) and returns a canned value."""

    def __init__(self, value: Any = "answer") -> None:
        self.calls: list[tuple[str | None, str | None]] = []
        self.slots: list[str | None] = []
        self.value = value

    async def __call__(self, target: Any) -> Any:
        self.calls.append((target.tier, target.preset))
        self.slots.append(current_llm_usage_slot())
        return {"tier": target.tier, "value": self.value}


class _Exploding:
    async def __call__(self, target: Any) -> Any:
        raise AssertionError("dispatch must not be called")


class _SpyPolicy:
    """Denies model.dispatch; records every decide call."""

    def __init__(self, deny: bool = True) -> None:
        self.seen: list[tuple[str, str]] = []
        self._deny = deny
        self._base = DefaultPolicy(deny_capabilities=frozenset({MODEL_DISPATCH}))

    def decide(self, principal: Any, req: Any, ctx: Any) -> Any:
        self.seen.append((req.capability, req.resource))
        return self._base.decide(principal, req, ctx) if self._deny else Allow()

    def attenuate(self, requested: Any, narrower: Any = None) -> Any:
        return self


def _events(sink: _RecordingSink, name: str) -> list[dict[str, Any]]:
    return [e for e in sink.events if e.get("event") == name]


def test_tier_order_matches_the_config_literal() -> None:
    assert TIER_ORDER == get_args(ModelTier)


def test_ladder_is_first_preset_per_tier_in_order() -> None:
    ladder = TierLadder.from_config(_ladder_config())
    assert ladder.tiers == ("local", "fast", "standard", "frontier")
    assert ladder.preset_for("standard") == "solid"
    assert ladder.next_above("fast") == "standard"
    assert ladder.next_above("frontier") is None


@pytest.mark.asyncio
async def test_solver_match_short_circuits_with_zero_dispatches(fresh_registry) -> None:
    fresh_registry.register("title", lambda p: Solved(p["text"].upper(), "upper"))
    sink = _RecordingSink()
    result = await route(
        "extract.title", "title", {"text": "hi"},
        config=_ladder_config(), dispatch=_Exploding(),
        env=_env(Path("/nonexistent-never-written"), sink),
    )
    assert result.value == "HI"
    assert result.solved_by == "upper"
    assert result.calls == 0
    assert result.tier is None
    (event,) = _events(sink, "model.route")
    assert event["check"] == "solver"


@pytest.mark.asyncio
async def test_solver_path_through_acomplete_json_makes_zero_provider_calls(
    fresh_registry, monkeypatch,
) -> None:
    import nanobot.providers.factory as factory

    def _make(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no provider may be built on the solver path")

    monkeypatch.setattr(factory, "make_provider", _make)
    fresh_registry.register("title", lambda p: Solved({"t": 1}, "fixed"))
    import nanobot.config.loader as loader

    def _no_config(**_k: Any) -> Any:
        raise AssertionError("a solved task must not load config")

    monkeypatch.setattr(loader, "config_from_sources", _no_config)
    result = await route("extract.title", "title", {"prompt": "x"})
    assert result.value == {"t": 1}
    assert result.calls == 0


@pytest.mark.asyncio
async def test_no_solver_and_no_verify_dispatches_once_at_fast(tmp_path: Path) -> None:
    dispatch = _Dispatcher()
    sink = _RecordingSink()
    result = await route(
        "extract.title", "title", {"prompt": "x"},
        config=_ladder_config(), dispatch=dispatch, env=_env(tmp_path, sink),
    )
    assert dispatch.calls == [("fast", "quick")]
    assert result.tier == "fast"
    assert result.preset == "quick"
    assert result.verified is None
    assert result.escalations == ()
    assert dispatch.slots == ["extract.title"]  # ledger events are attributed to the slot
    (event,) = _events(sink, "model.route")
    assert event["reason"] == "initial"
    assert event["check"] == "no_ceiling"


@pytest.mark.asyncio
async def test_verify_true_does_not_escalate() -> None:
    dispatch = _Dispatcher()
    result = await route(
        "extract.title", None, {"prompt": "x"},
        config=_ladder_config(), dispatch=dispatch, verify=lambda v: True,
    )
    assert dispatch.calls == [("fast", "quick")]
    assert result.verified is True
    assert result.escalations == ()


@pytest.mark.asyncio
async def test_verify_false_escalates_exactly_one_tier_and_records_why(tmp_path: Path) -> None:
    dispatch = _Dispatcher()
    sink = _RecordingSink()
    seen: list[Any] = []

    def _verify(value: Any) -> bool:
        seen.append(value["tier"])
        return value["tier"] != "fast"

    result = await route(
        "extract.title", None, {"prompt": "x"},
        config=_ladder_config(), dispatch=dispatch, verify=_verify, env=_env(tmp_path, sink),
    )
    # fast -> standard, never straight to frontier
    assert dispatch.calls == [("fast", "quick"), ("standard", "solid")]
    assert seen == ["fast", "standard"]
    assert result.tier == "standard"
    assert result.verified is True
    (esc,) = result.escalations
    assert (esc.from_tier, esc.to_tier) == ("fast", "standard")
    assert esc.reason == "verification_failed"
    assert esc.justified is True
    routed = _events(sink, "model.route")
    assert [e["reason"] for e in routed] == ["initial", "verification_failed"]
    assert routed[1]["from_tier"] == "fast"


@pytest.mark.asyncio
async def test_escalation_is_bounded_by_max_escalations() -> None:
    dispatch = _Dispatcher()
    result = await route(
        "extract.title", None, {"prompt": "x"},
        config=_ladder_config(), dispatch=dispatch, verify=lambda v: False,
    )
    assert dispatch.calls == [("fast", "quick"), ("standard", "solid")]
    assert result.verified is False
    assert len(result.escalations) == 1


@pytest.mark.asyncio
async def test_async_verify_and_raising_verify_count_as_failure() -> None:
    async def _averify(value: Any) -> bool:
        return value["tier"] == "standard"

    result = await route(
        "s", None, {}, config=_ladder_config(), dispatch=_Dispatcher(), verify=_averify,
    )
    assert result.tier == "standard" and result.verified is True

    def _boom(value: Any) -> bool:
        raise RuntimeError("checker broke")

    result = await route(
        "s", None, {}, config=_ladder_config(), dispatch=_Dispatcher(), verify=_boom,
        max_escalations=0,
    )
    assert result.verified is False
    assert result.attempts[0].verified is False


@pytest.mark.asyncio
async def test_top_tier_failure_has_nowhere_to_go() -> None:
    dispatch = _Dispatcher()
    result = await route(
        "s", None, {}, config=_ladder_config(), dispatch=dispatch, tier="frontier",
        verify=lambda v: False,
    )
    assert dispatch.calls == [("frontier", "big")]
    assert result.verified is False
    assert result.escalations == ()


@pytest.mark.asyncio
async def test_untiered_config_dispatches_the_active_preset() -> None:
    dispatch = _Dispatcher()
    config = Config.model_validate({"providers": {"openai": {"apiKey": "sk-x"}}})
    result = await route("s", None, {}, config=config, dispatch=dispatch)
    assert dispatch.calls == [(None, None)]
    assert result.tier is None


# -- model.dispatch --------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_ceiling_dispatch_without_failure_is_denied_by_policy(tmp_path: Path) -> None:
    sink = _RecordingSink()
    env = _env(tmp_path, sink)
    policy = _SpyPolicy(deny=True)
    result = await route(
        "extract.title", None, {"prompt": "x"},
        config=_ladder_config(ceiling="fast"), dispatch=_Exploding(), tier="frontier",
        policy=policy, env=env,
    )
    assert result.denied
    assert result.calls == 0
    assert policy.seen == [(MODEL_DISPATCH, "frontier")]
    deny = result.denial.deny
    assert deny.marker == POLICY_MARKER
    assert deny.capability == MODEL_DISPATCH
    assert POLICY_MARKER in result.error_text()
    # A genuine policy-layer deny, classified like any other (violation:policy:*).
    assert result.denial.layer == "policy"
    assert result.denial.policy_capability == MODEL_DISPATCH
    # Audited: the gate's policy.decision event and the router's own event.
    (decision,) = _events(sink, "policy.decision")
    assert decision["capability"] == MODEL_DISPATCH
    assert decision["resource"] == "frontier"
    assert decision["verdict"] == "deny"
    (routed,) = _events(sink, "model.route")
    assert (routed["check"], routed["verdict"]) == ("policy", "deny")
    # And logged as a deferred action (design 5a), like every gate denial.
    assert result.denial.deferred is True
    lines = deferred_log_path(env.paths.work_dir).read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["tool"] == MODEL_DISPATCH
    assert entry["capability"] == MODEL_DISPATCH


@pytest.mark.asyncio
async def test_over_ceiling_dispatch_without_a_policy_is_denied() -> None:
    """A configured ceiling is enforced unless the host hands in a policy that grants it."""
    result = await route(
        "extract.title", None, {}, config=_ladder_config(ceiling="fast"),
        dispatch=_Exploding(), tier="frontier",
    )
    assert result.denied
    assert result.denial.deny.capability == MODEL_DISPATCH


@pytest.mark.asyncio
async def test_over_ceiling_dispatch_granted_by_policy_proceeds_audited(tmp_path: Path) -> None:
    sink = _RecordingSink()
    dispatch = _Dispatcher()
    result = await route(
        "extract.title", None, {}, config=_ladder_config(ceiling="fast"),
        dispatch=dispatch, tier="frontier", policy=DefaultPolicy(), env=_env(tmp_path, sink),
    )
    assert not result.denied
    assert dispatch.calls == [("frontier", "big")]
    (decision,) = _events(sink, "policy.decision")
    assert decision["verdict"] == "allow"
    (routed,) = _events(sink, "model.route")
    assert (routed["check"], routed["verdict"]) == ("policy", "allow")


@pytest.mark.asyncio
async def test_justified_escalation_above_ceiling_skips_policy_but_is_audited(
    tmp_path: Path,
) -> None:
    sink = _RecordingSink()
    policy = _SpyPolicy(deny=True)
    dispatch = _Dispatcher()
    result = await route(
        "extract.title", None, {}, config=_ladder_config(ceiling="fast"),
        dispatch=dispatch, verify=lambda v: v["tier"] == "standard",
        policy=policy, env=_env(tmp_path, sink),
    )
    assert dispatch.calls == [("fast", "quick"), ("standard", "solid")]
    assert policy.seen == []  # never asked: a recorded failure justifies it
    assert result.verified is True
    assert _events(sink, "policy.decision") == []
    routed = _events(sink, "model.route")
    assert [(e["check"], e["verdict"]) for e in routed] == [
        ("within_ceiling", "allow"), ("justified", "allow"),
    ]
    assert routed[1]["ceiling"] == "fast"


@pytest.mark.asyncio
async def test_no_ceiling_means_no_model_dispatch_check(tmp_path: Path) -> None:
    sink = _RecordingSink()
    policy = _SpyPolicy(deny=True)
    dispatch = _Dispatcher()
    result = await route(
        "extract.title", None, {}, config=_ladder_config(), dispatch=dispatch,
        tier="frontier", policy=policy, env=_env(tmp_path, sink),
    )
    assert dispatch.calls == [("frontier", "big")]
    assert not result.denied
    assert policy.seen == []
    assert _events(sink, "policy.decision") == []


@pytest.mark.asyncio
async def test_default_start_is_clamped_to_the_ceiling() -> None:
    policy = _SpyPolicy(deny=True)
    dispatch = _Dispatcher()
    await route(
        "extract.title", None, {}, config=_ladder_config(ceiling="local"),
        dispatch=dispatch, policy=policy,
    )
    assert dispatch.calls == [("local", "tiny")]
    assert policy.seen == []


@pytest.mark.asyncio
async def test_configured_start_above_ceiling_is_checked() -> None:
    policy = _SpyPolicy(deny=True)
    result = await route(
        "extract.title", None, {}, config=_ladder_config(ceiling="fast", start="standard"),
        dispatch=_Exploding(), policy=policy,
    )
    assert result.denied
    assert policy.seen == [(MODEL_DISPATCH, "standard")]


@pytest.mark.asyncio
async def test_unknown_explicit_tier_is_an_error() -> None:
    config = Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-x"}},
        "modelPresets": {"quick": {"provider": "openai", "model": "m", "tier": "fast"}},
    })
    with pytest.raises(ValueError, match="frontier"):
        await route("s", None, {}, config=config, dispatch=_Dispatcher(), tier="frontier")


# -- composition with acomplete_json / think_structured ------------------------------


class _StubProvider:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
        return SimpleNamespace(content=self._replies.pop(0), finish_reason="stop", error_type=None)


def _install(monkeypatch, replies: list[str]) -> list[dict[str, Any]]:
    import nanobot.providers.factory as factory

    made: list[dict[str, Any]] = []
    provider = _StubProvider(replies)

    def _make(config, *, preset_name=None, preset=None, model=None, **kw):
        made.append({"preset_name": preset_name, **kw})
        return provider

    monkeypatch.setattr(factory, "make_provider", _make)
    return made


@pytest.mark.asyncio
async def test_default_dispatch_goes_through_acomplete_json_with_the_tier_preset(
    monkeypatch, tmp_path: Path,
) -> None:
    made = _install(monkeypatch, ['{"ok": false}', '{"ok": true}'])
    env = _env(tmp_path, _RecordingSink())
    result = await route(
        "extract.title", "title", {"prompt": "x"}, prompt="x",
        config=_ladder_config(), verify=lambda v: v["ok"], env=env,
    )
    assert result.value == {"ok": True}
    assert [m["preset_name"] for m in made] == ["quick", "solid"]
    assert all(m["env"] is env for m in made)  # the ledger is wired for routed calls


@pytest.mark.asyncio
async def test_default_dispatch_does_not_warn_kernel_internal(monkeypatch, tmp_path: Path) -> None:
    """Task 12 regression: ``_default_dispatch`` calls the unwarned
    ``_acomplete_json_impl``, never the public (deprecated) ``acomplete_json`` —
    the router is kernel-internal code and must never trigger that
    ``DeprecationWarning`` (ruling 3). This is the same path
    ``test_default_dispatch_goes_through_acomplete_json_with_the_tier_preset``
    exercises above; here the assertion is the *absence* of a warning.
    """
    _install(monkeypatch, ['{"ok": true}'])
    env = _env(tmp_path, _RecordingSink())
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = await route(
            "extract.title", "title", {"prompt": "x"}, prompt="x",
            config=_ladder_config(), env=env,
        )
    assert result.value == {"ok": True}


@pytest.mark.asyncio
async def test_think_structured_routes_when_slot_given(monkeypatch) -> None:
    made = _install(monkeypatch, ['{"a": 1}'])
    out = await MoekaCore.think_structured(
        "x", slot="extract.title", config=_ladder_config(), verify=lambda v: True,
    )
    assert out == {"a": 1}
    assert [m["preset_name"] for m in made] == ["quick"]


@pytest.mark.asyncio
async def test_think_structured_raises_on_model_dispatch_denial(monkeypatch) -> None:
    _install(monkeypatch, [])
    with pytest.raises(ModelDispatchDeniedError) as info:
        await MoekaCore.think_structured(
            "x", slot="extract.title", config=_ladder_config(ceiling="fast"), tier="frontier",
        )
    assert POLICY_MARKER in str(info.value)
    assert info.value.result.denied


@pytest.mark.asyncio
async def test_think_structured_raises_when_verification_never_passes(monkeypatch) -> None:
    _install(monkeypatch, ['{"a": 1}', '{"a": 2}'])
    with pytest.raises(RouteVerificationError) as info:
        await MoekaCore.think_structured(
            "x", slot="s", config=_ladder_config(), verify=lambda v: False,
        )
    assert info.value.result.value == {"a": 2}


@pytest.mark.asyncio
async def test_think_structured_without_slot_is_unchanged(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(prompt, **kw):
        seen.update(kw)
        return "plain"

    monkeypatch.setattr(complete_mod, "acomplete_json", fake)

    def _no_route(*a: Any, **k: Any) -> Any:
        raise AssertionError("route must not run without slot/verify/tier")

    monkeypatch.setattr(router_mod, "route", _no_route)
    assert await MoekaCore.think_structured("x", schema={"type": "object"}) == "plain"
    assert set(seen) == {"schema", "model_cls", "retries"}


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # calls the real, unmocked acomplete
async def test_acomplete_without_env_keeps_the_old_make_provider_call(monkeypatch) -> None:
    made = _install(monkeypatch, ["hi"])
    await complete_mod.acomplete("x", config=_ladder_config())
    assert made == [{"preset_name": None}]


# ================================================================================
# Baseline comparators (the RSI harness's I6 score)
# ================================================================================


def _event(cost: float | None, usage_source: str = "reported", tier: str | None = "fast") -> Any:
    return LedgerEvent(
        trace_id=None, slot="s", tier=tier, model="m", provider="p",
        tokens_in=1, tokens_out=1, tokens_cache_read=0, latency_ms=1.0,
        cost_usd=cost, source="system", usage_source=usage_source,
    )


_ZERO_SHOT = Baseline("extract.title", "one zero-shot fast-tier call", reference_cost_usd=0.002)


def test_cost_ratio_of_known_events_against_a_known_baseline() -> None:
    assert cost_ratio([_event(0.001), _event(0.002)], _ZERO_SHOT) == pytest.approx(1.5)


def test_cost_ratio_accepts_trace_dicts_from_a_fake_ledger() -> None:
    events = [_event(0.001).to_trace(), _event(0.003).to_trace()]
    assert cost_ratio(events, _ZERO_SHOT) == pytest.approx(2.0)


def test_cost_ratio_with_no_calls_is_zero() -> None:
    assert cost_ratio([], _ZERO_SHOT) == 0.0


def test_cost_ratio_unknown_cost_is_none() -> None:
    comparison = compare_cost([_event(0.001), _event(None)], _ZERO_SHOT)
    assert comparison.ratio is None
    assert comparison.reason == "unknown_cost"
    assert cost_ratio([_event(None)], _ZERO_SHOT) is None


def test_cost_ratio_refuses_estimated_usage_by_default() -> None:
    events = [_event(0.001), _event(0.001, usage_source="estimated")]
    comparison = compare_cost(events, _ZERO_SHOT)
    assert comparison.ratio is None
    assert comparison.reason == "estimated_usage"
    assert comparison.unbilled == 1
    assert cost_ratio(events, _ZERO_SHOT) is None
    # Opt-in: the harness may accept estimates, and the result says so.
    flagged = compare_cost(events, _ZERO_SHOT, allow_estimated=True)
    assert flagged.ratio == pytest.approx(1.0)
    assert flagged.estimated is True


def test_old_trace_dicts_without_usage_source_are_not_billed() -> None:
    old = {k: v for k, v in _event(0.001).to_trace().items() if k != "usage_source"}
    assert cost_ratio([old], _ZERO_SHOT) is None


def test_local_zero_cost_counts_whatever_the_usage_source() -> None:
    assert cost_ratio([_event(0.0, usage_source="estimated", tier="local")], _ZERO_SHOT) == 0.0


def test_deterministic_baseline_is_free() -> None:
    parser = Baseline.deterministic("parse.version", "a regex over the version string")
    assert parser.reference_cost_usd == 0.0
    assert cost_ratio([], parser) == 1.0  # parity: the solver path made no call
    assert cost_ratio([_event(0.0001)], parser) == math.inf  # any spend is an I6 violation


def test_unknown_baseline_cost_is_none() -> None:
    assert cost_ratio([_event(0.001)], Baseline("f", "a static heuristic")) is None


def test_baseline_registry_declares_one_alternative_per_family(monkeypatch) -> None:
    registry = BaselineRegistry()
    registry.register(_ZERO_SHOT)
    assert registry.get("extract.title") is _ZERO_SHOT
    assert registry.get("missing") is None
    with pytest.raises(ValueError, match="already declared"):
        registry.register(Baseline("extract.title", "other"))
    registry.register(Baseline("extract.title", "other"), replace=True)
    assert registry.get("extract.title").alternative == "other"
    assert registry.families() == ("extract.title",)

    fresh = BaselineRegistry()
    monkeypatch.setattr(baselines_mod, "_DEFAULT_REGISTRY", fresh)
    baselines_mod.declare_baseline(_ZERO_SHOT)
    assert baselines_mod.get_baseline("extract.title") is _ZERO_SHOT


def test_baseline_rejects_bad_declarations() -> None:
    with pytest.raises(ValueError):
        Baseline("", "x")
    with pytest.raises(ValueError):
        Baseline("f", "x", reference_cost_usd=-1.0)
