"""Helpers shared by the held-out tests and their worker processes (public surfaces only)."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

MAIN_PRICE_IN = 1.0
MAIN_PRICE_OUT = 2.0


class Sink:
    """A trace sink that records every event."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


def main_spec():
    from moeka import ModelSpec

    return ModelSpec(
        name="main", model="fake-main", provider="openai",
        price_in=MAIN_PRICE_IN, price_out=MAIN_PRICE_OUT, max_tokens=100,
    )


def make_env(state: Path | str, work: Path | str, *, data_dir: Path | str | None = None,
             trace: Any = None, strict: bool = True):
    from moeka import Environment, ModelSpec, ProviderSpec

    kwargs: dict[str, Any] = {}
    if data_dir is not None:
        kwargs["data_dir"] = data_dir
    return Environment.for_host(
        state_dir=state, work_dir=work,
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main", trace=trace, strict=strict, offline=True, **kwargs,
    )


def attach_fake(kernel, *script: Any):
    from moeka.testing import FakeProvider, reply

    fake = FakeProvider(list(script), default=reply("ok", input_tokens=20, output_tokens=5))
    kernel.llm.register_provider("main", fake, main_spec())
    return fake


def estimate(call_id: str, usd: float | None, tokens: int = 100, tags: dict | None = None):
    from moeka.budget import CallEstimate

    return CallEstimate(
        call_id=call_id, alias="main", model="fake-main", provider="openai",
        prompt_tokens=tokens // 2, max_output_tokens=tokens - tokens // 2, rounds=1,
        worst_case_tokens=tokens, worst_case_usd=usd, tags=dict(tags or {}),
    )


def event(cost: float | None, *, tokens_in: int = 10, tokens_out: int = 5,
          outcome: str = "ok", call_id: str | None = None, tags: dict | None = None):
    from moeka.budget import ModelCallEvent

    kwargs: dict[str, Any] = dict(
        trace_id=None, slot=None, tier=None, model="fake-main", provider="openai",
        tokens_in=tokens_in, tokens_out=tokens_out, tokens_cache_read=None,
        latency_ms=1.0, cost_usd=cost, source="user", call_id=call_id, outcome=outcome,
    )
    params = inspect.signature(ModelCallEvent).parameters
    if tags is not None and "tags" in params:
        kwargs["tags"] = dict(tags)
    return ModelCallEvent(**{k: v for k, v in kwargs.items() if k in params})


def shared_budget(data_dir: Path | str, budget_id: str = "default", **kw: Any):
    from moeka.budget import SharedCapBudget

    return SharedCapBudget(data_dir, budget_id, **kw)
