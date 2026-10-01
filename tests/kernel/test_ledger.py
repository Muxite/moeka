"""Task 14: the cost/usage ledger (I6 measurement, design section 6).

Every physical LLM call becomes one ``model.call`` ledger event on the env's
``TraceSink`` and one row in ``LLMUsageStore``: tokens, tier, latency, cost.
The ledger is pure instrumentation: a sink or store failure never fails the call.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.ledger import LedgerEvent, LedgerObserver, ModelPricing, compute_cost
from nanobot.llm_usage.context import (
    current_llm_usage_slot,
    current_llm_usage_trace_id,
    llm_usage_slot,
    llm_usage_source,
    llm_usage_trace_id,
)
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import SCHEMA_VERSION, LLMUsageStore
from nanobot.providers.base import LLMResponse, LLMUsage
from nanobot.providers.factory import make_provider

# Recent enough to survive the store's 400-day retention prune.
_NOW_MS = int(time.time() * 1000)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _RaisingSink:
    def emit(self, event: dict[str, Any]) -> None:
        raise RuntimeError("sink down")


class _RaisingStore:
    def __init__(self) -> None:
        self.calls = 0

    def record(self, call: LLMCallRecord) -> None:
        self.calls += 1
        raise sqlite3.OperationalError("database is locked")


class _RecordingStore:
    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []

    def record(self, call: LLMCallRecord) -> None:
        self.records.append(call)


def _record(
    *,
    model: str = "gpt-5-mini",
    usage: LLMUsage | None = None,
    duration_ms: int = 420,
) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=_NOW_MS,
        duration_ms=duration_ms,
        provider="openai",
        model=model,
        source="user",
        stream=False,
        finish_reason="stop",
        usage=usage,
    )


# -- compute_cost -----------------------------------------------------------


def test_cost_is_tokens_times_price() -> None:
    usage = LLMUsage.reported(input_tokens=1_000_000, output_tokens=500_000)
    assert compute_cost(usage, 2.0, 8.0, None) == pytest.approx(2.0 + 4.0)


def test_cost_bills_cache_reads_at_the_cache_price() -> None:
    usage = LLMUsage.reported(
        input_tokens=1_000_000, output_tokens=0, cache_read_tokens=400_000
    )
    # 600k uncached at 3.0 + 400k cached at 0.3
    assert compute_cost(usage, 3.0, 15.0, 0.3) == pytest.approx(1.8 + 0.12)


def test_missing_input_price_yields_none_not_zero() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10)
    assert compute_cost(usage, None, 8.0, None) is None


def test_missing_output_price_yields_none_not_zero() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10)
    assert compute_cost(usage, 2.0, None, None) is None


def test_missing_cache_read_price_with_cache_reads_yields_none() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10, cache_read_tokens=50)
    assert compute_cost(usage, 2.0, 8.0, None) is None


def test_no_prices_and_no_tier_yields_none() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10)
    assert compute_cost(usage, None, None, None) is None


def test_local_tier_without_prices_costs_zero() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10)
    assert compute_cost(usage, None, None, None, tier="local") == 0.0


def test_local_tier_with_explicit_prices_uses_the_prices() -> None:
    usage = LLMUsage.reported(input_tokens=1_000_000, output_tokens=0)
    assert compute_cost(usage, 0.5, 0.5, None, tier="local") == pytest.approx(0.5)


def test_non_local_tier_without_prices_is_unknown() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=10)
    assert compute_cost(usage, None, None, None, tier="fast") is None


def test_missing_usage_yields_none() -> None:
    assert compute_cost(None, 2.0, 8.0, 0.2) is None


# -- config -------------------------------------------------------------------


def test_preset_pricing_fields_default_to_none() -> None:
    preset = ModelPresetConfig(model="m")
    assert preset.tier is None
    assert preset.price_in_per_mtok is None
    assert preset.price_out_per_mtok is None
    assert preset.price_cache_read_per_mtok is None


def test_preset_pricing_fields_accept_camel_case() -> None:
    preset = ModelPresetConfig.model_validate({
        "model": "m",
        "tier": "frontier",
        "priceInPerMtok": 3,
        "priceOutPerMtok": 15,
        "priceCacheReadPerMtok": 0.3,
    })
    assert preset.tier == "frontier"
    assert preset.price_in_per_mtok == 3
    assert preset.price_out_per_mtok == 15
    assert preset.price_cache_read_per_mtok == 0.3


def test_preset_rejects_unknown_tier_and_negative_price() -> None:
    with pytest.raises(ValueError):
        ModelPresetConfig.model_validate({"model": "m", "tier": "gigantic"})
    with pytest.raises(ValueError):
        ModelPresetConfig.model_validate({"model": "m", "priceInPerMtok": -1})


# -- context vars -------------------------------------------------------------


def test_slot_and_trace_id_default_to_none() -> None:
    assert current_llm_usage_slot() is None
    assert current_llm_usage_trace_id() is None


def test_slot_and_trace_id_bind_and_reset() -> None:
    with llm_usage_slot("extract.title"), llm_usage_trace_id("trace-1"):
        assert current_llm_usage_slot() == "extract.title"
        assert current_llm_usage_trace_id() == "trace-1"
    assert current_llm_usage_slot() is None
    assert current_llm_usage_trace_id() is None


# -- LedgerObserver -------------------------------------------------------------


def test_observer_maps_record_to_event() -> None:
    sink = _RecordingSink()
    store = _RecordingStore()
    observer = LedgerObserver(
        sink=sink,
        store=store,
        pricing={"gpt-5-mini": ModelPricing(tier="fast", price_in=0.25, price_out=2.0)},
    )
    usage = LLMUsage.reported(input_tokens=2_000, output_tokens=500, cache_read_tokens=0)
    with llm_usage_source("cron"), llm_usage_slot("s1"), llm_usage_trace_id("t1"):
        record = LLMCallRecord(
            started_at_ms=1,
            duration_ms=333,
            provider="openai",
            model="gpt-5-mini",
            source="cron",
            stream=True,
            finish_reason="stop",
            usage=usage,
        )
        event = observer.event_for(record)
        observer(record)

    assert event == LedgerEvent(
        trace_id="t1",
        slot="s1",
        tier="fast",
        model="gpt-5-mini",
        provider="openai",
        tokens_in=2_000,
        tokens_out=500,
        tokens_cache_read=0,
        latency_ms=333.0,
        cost_usd=pytest.approx(2_000 * 0.25 / 1e6 + 500 * 2.0 / 1e6),
        source="cron",
        usage_source="reported",
        finish_reason="stop",
        price_source="price_table",
        started_at_ms=1,
        tokens_cache_write=None,
    )
    assert len(sink.events) == 1
    emitted = sink.events[0]
    assert emitted["event"] == "model.call"
    assert emitted["tier"] == "fast"
    assert emitted["slot"] == "s1"
    assert emitted["trace_id"] == "t1"
    assert len(store.records) == 1
    assert store.records[0].tier == "fast"
    assert store.records[0].cost_usd == pytest.approx(event.cost_usd)


def test_observer_without_pricing_leaves_tier_and_cost_none() -> None:
    sink = _RecordingSink()
    observer = LedgerObserver(sink=sink)
    observer(_record(usage=LLMUsage.reported(input_tokens=10, output_tokens=2)))
    assert len(sink.events) == 1
    assert sink.events[0]["tier"] is None
    assert sink.events[0]["cost_usd"] is None
    assert sink.events[0]["tokens_in"] == 10


def test_observer_handles_missing_usage() -> None:
    sink = _RecordingSink()
    LedgerObserver(sink=sink)(_record(usage=None))
    event = sink.events[0]
    assert (event["tokens_in"], event["tokens_out"]) == (0, 0)
    assert event["tokens_cache_read"] is None  # no usage: unknown, not zero
    assert event["cost_usd"] is None


def test_raising_sink_does_not_propagate_and_store_still_records() -> None:
    store = _RecordingStore()
    observer = LedgerObserver(sink=_RaisingSink(), store=store)
    observer(_record(usage=LLMUsage.reported(input_tokens=1, output_tokens=1)))
    assert len(store.records) == 1


def test_raising_store_does_not_propagate_and_sink_still_emits() -> None:
    sink = _RecordingSink()
    store = _RaisingStore()
    observer = LedgerObserver(sink=sink, store=store)
    observer(_record(usage=LLMUsage.reported(input_tokens=1, output_tokens=1)))
    assert store.calls == 1
    assert len(sink.events) == 1


def test_broken_pricing_does_not_propagate() -> None:
    class _BadPricing(dict):
        def get(self, *_a: object, **_k: object) -> Any:
            raise RuntimeError("pricing lookup broke")

    observer = LedgerObserver(sink=_RaisingSink(), store=_RaisingStore(), pricing=_BadPricing())
    observer(_record(usage=LLMUsage.reported(input_tokens=1, output_tokens=1)))


# -- store schema migration ---------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    stream INTEGER NOT NULL,
    finish_reason TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reported_tokens INTEGER,
    estimated_tokens INTEGER,
    generation_ms INTEGER,
    measured_output_tokens INTEGER,
    ttft_ms INTEGER,
    timed_requests INTEGER,
    error_status_code INTEGER,
    error_kind TEXT
);
"""


def _columns(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection, connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(llm_calls)")}


def test_schema_version_is_bumped() -> None:
    assert SCHEMA_VERSION == 4


def test_v1_database_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_V1_SCHEMA)
        connection.execute(
            "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, "
            "stream, finish_reason, input_tokens, output_tokens, total_tokens) "
            "VALUES (?, 5, 'openai', 'old-model', 'user', 0, 'stop', 7, 3, 10)",
            (_NOW_MS,),
        )
        connection.execute("PRAGMA user_version = 1")
    assert "tier" not in _columns(path)

    store = LLMUsageStore(path)
    store.record(
        LLMCallRecord(
            started_at_ms=_NOW_MS + 1_000,
            duration_ms=9,
            provider="openai",
            model="new-model",
            source="user",
            stream=False,
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
            tier="standard",
            cost_usd=0.5,
        )
    )
    store.close()

    assert {"tier", "cost_usd"} <= _columns(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    reader = LLMUsageStore(path)
    rows = {row["model"]: row for row in reader.recent_calls(limit=10)}
    reader.close()
    assert rows["old-model"]["total_tokens"] == 10
    assert rows["old-model"]["tier"] is None
    assert rows["old-model"]["cost_usd"] is None
    assert rows["new-model"]["tier"] == "standard"
    assert rows["new-model"]["cost_usd"] == pytest.approx(0.5)


def test_migration_is_idempotent_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_V1_SCHEMA)
        connection.execute("PRAGMA user_version = 1")
    for _ in range(2):
        store = LLMUsageStore(path)
        assert store.count() == 0
        store.close()
    assert {"tier", "cost_usd"} <= _columns(path)


def test_new_database_has_ledger_columns(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.sqlite3"
    store = LLMUsageStore(path)
    store.record(_record(usage=LLMUsage.reported(input_tokens=2, output_tokens=1)))
    row = store.recent_calls(limit=1)[0]
    store.close()
    assert row["tier"] is None
    assert row["cost_usd"] is None
    assert {"tier", "cost_usd"} <= _columns(path)


# -- end to end: factory-wired observer -----------------------------------------


def _env(tmp_path: Path, sink: _RecordingSink) -> CoreEnvironment:
    return CoreEnvironment(
        config=type("_Empty", (), {"section": lambda self, name: {}})(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink,
    )


def _config(tmp_path: Path, preset: dict[str, Any]) -> Config:
    return Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-ledger-test"}},
        "modelPresets": {"work": {"provider": "openai", **preset}},
        "agents": {"defaults": {"workspace": str(tmp_path / "ws"), "modelPreset": "work"}},
    })


async def _one_call(provider: Any, model: str, *, leaf: Any = None) -> LLMResponse:
    async def _fake_chat(**_kwargs: object) -> LLMResponse:
        return LLMResponse(
            content="ok",
            usage=LLMUsage.reported(input_tokens=1_000, output_tokens=200),
        )

    (leaf if leaf is not None else provider).chat = _fake_chat
    return await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model=model
    )


@pytest.mark.asyncio
async def test_factory_wires_one_priced_ledger_event_per_call(tmp_path: Path) -> None:
    sink = _RecordingSink()
    env = _env(tmp_path, sink)
    config = _config(tmp_path, {
        "model": "gpt-5-mini",
        "tier": "fast",
        "priceInPerMtok": 0.25,
        "priceOutPerMtok": 2.0,
    })
    provider = make_provider(config, env=env)

    response = await _one_call(provider, "gpt-5-mini")

    assert response.content == "ok"
    ledger = [e for e in sink.events if e.get("event") == "model.call"]
    assert len(ledger) == 1
    assert ledger[0]["tier"] == "fast"
    assert ledger[0]["tokens_in"] == 1_000
    assert ledger[0]["tokens_out"] == 200
    assert ledger[0]["cost_usd"] == pytest.approx(1_000 * 0.25 / 1e6 + 200 * 2.0 / 1e6)
    reader = LLMUsageStore(env.paths.data_dir / "llm_usage.sqlite3")
    rows = reader.recent_calls(limit=5)
    reader.close()
    assert len(rows) == 1
    assert rows[0]["tier"] == "fast"
    assert rows[0]["cost_usd"] == pytest.approx(ledger[0]["cost_usd"])


@pytest.mark.asyncio
async def test_factory_event_fires_without_pricing(tmp_path: Path) -> None:
    sink = _RecordingSink()
    provider = make_provider(_config(tmp_path, {"model": "gpt-5-mini"}), env=_env(tmp_path, sink))

    await _one_call(provider, "gpt-5-mini")

    ledger = [e for e in sink.events if e.get("event") == "model.call"]
    assert len(ledger) == 1
    assert ledger[0]["tier"] is None
    assert ledger[0]["cost_usd"] is None
    assert ledger[0]["tokens_in"] == 1_000


@pytest.mark.asyncio
async def test_factory_ledger_failure_never_fails_the_call(tmp_path: Path) -> None:
    env = CoreEnvironment(
        config=type("_Empty", (), {"section": lambda self, name: {}})(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(
            work_dir=tmp_path / "work",
            state_dir=tmp_path / "state",
            # a file where the data dir should be: the store cannot open
            data_dir_override=tmp_path / "blocked" / "data",
        ),
        trace=_RaisingSink(),
    )
    (tmp_path / "blocked").write_text("not a directory")
    provider = make_provider(_config(tmp_path, {"model": "gpt-5-mini"}), env=env)

    response = await _one_call(provider, "gpt-5-mini")

    assert response.content == "ok"
    assert response.finish_reason == "stop"


def test_factory_without_env_attaches_no_observer(tmp_path: Path) -> None:
    provider = make_provider(_config(tmp_path, {"model": "gpt-5-mini"}))
    assert provider._llm_call_observer is None


@pytest.mark.asyncio
async def test_fallback_chain_records_one_event_per_physical_call(tmp_path: Path) -> None:
    sink = _RecordingSink()
    config = Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-ledger-test"}},
        "modelPresets": {
            "work": {"provider": "openai", "model": "gpt-5", "tier": "frontier"},
            "cheap": {"provider": "openai", "model": "gpt-5-mini", "tier": "fast"},
        },
        "agents": {"defaults": {
            "workspace": str(tmp_path / "ws"),
            "modelPreset": "work",
            "fallbackModels": ["cheap"],
        }},
    })
    provider = make_provider(config, env=_env(tmp_path, sink))
    assert type(provider).__name__ == "FallbackProvider"

    response = await _one_call(provider, "gpt-5", leaf=provider._primary)

    assert response.content == "ok"

    ledger = [e for e in sink.events if e.get("event") == "model.call"]
    assert len(ledger) == 1
    assert ledger[0]["tier"] == "frontier"
