"""Spec 001: the consumer usage surface (``kernel.usage``) and its agreements.

Attribution on every event, cache hits accounted, budget snapshots, live subscription that
never blocks a call, stream totals equal to stored totals, and events that validate
against the canonical JSON Schemas in ``schemas/``.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

import pytest

from moeka.budget import CapBudget
from moeka.errors import BudgetExceeded
from moeka.llm import Completion, GenerateOptions, user
from moeka.testing import FakeProvider, reply
from moeka.usage import (
    SCHEMA_VERSION,
    UNATTRIBUTED,
    Attribution,
    UsageFilter,
    bind_attribution,
)
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.ledger import LedgerObserver
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.providers.base import LLMUsage

jsonschema = pytest.importorskip("jsonschema")
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _validator(name: str) -> Any:
    schema = json.loads((SCHEMAS / f"{name}.v1.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, Completion] = {}

    def get(self, key: str) -> Completion | None:
        return self.data.get(key)

    def put(self, key: str, completion: Completion) -> None:
        self.data[key] = completion


MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(*script: Any, **kwargs: Any) -> tuple[Kernel, FakeProvider]:
        env = Environment.for_host(
            state_dir=tmp_path / "state", work_dir=tmp_path / "work",
            credentials={"oa": "sk-test"},
            providers=[ProviderSpec(name="openai", credential="oa")],
            models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
            default_model="main", trace=sink,
        )
        kwargs.setdefault("consumer", "test-app")
        kernel = Kernel(env, **kwargs)
        kernels.append(kernel)
        fake = FakeProvider(script, default=reply("ok", input_tokens=20, output_tokens=5))
        kernel.llm.register_provider("main", fake, MAIN)
        return kernel, fake

    yield make
    for kernel in kernels:
        kernel.close()


def _opts(**who: str) -> GenerateOptions:
    return GenerateOptions(attribution=Attribution(**who))


# -- attribution ------------------------------------------------------------------------


async def test_call_events_carry_typed_attribution(make_kernel, sink) -> None:
    kernel, _ = make_kernel()
    await kernel.llm.generate(
        [user("hi")], _opts(agent="writer", session="s1", role="maker", purpose="draft"),
    )
    [event] = sink.named("model.call")
    assert (event["consumer"], event["agent"], event["session"], event["role"],
            event["purpose"]) == ("test-app", "writer", "s1", "maker", "draft")
    assert event["call_id"] and event["attempt"] == 1
    assert event["schema_version"] == SCHEMA_VERSION and event["kind"] == "model_call"
    assert event["record_id"] == f"{event['call_id']}:1"
    _validator("usage-record").validate(event)


async def test_ambient_binding_and_opts_precedence(make_kernel, sink) -> None:
    kernel, _ = make_kernel()
    with bind_attribution(Attribution(agent="outer", role="r1", session="s9")):
        await kernel.llm.generate([user("a")])
        await kernel.llm.generate([user("b")], _opts(role="r2"))
    first, second = sink.named("model.call")
    assert (first["agent"], first["role"], first["session"]) == ("outer", "r1", "s9")
    assert (second["agent"], second["role"], second["session"]) == ("outer", "r2", "s9")


async def test_unattributed_is_visible_not_blank(make_kernel, sink) -> None:
    kernel, _ = make_kernel(consumer=None)
    await kernel.llm.generate([user("a")])
    assert sink.named("model.call")[0]["consumer"] == UNATTRIBUTED


async def test_direct_provider_call_gets_identity() -> None:
    """A call made outside the LLM layer still has a call id, attempt and consumer field."""
    events: list[dict[str, Any]] = []
    sink = type("S", (), {"emit": lambda self, e: events.append(e)})()
    observer = LedgerObserver(sink=sink)
    record = LLMCallRecord(
        started_at_ms=1, duration_ms=2, provider="p", model="m", source="user",
        stream=False, finish_reason="stop", usage=LLMUsage.reported(input_tokens=1, output_tokens=1),
    )
    with bind_attribution(Attribution(consumer="c", agent="loop", session="k")):
        observer(record)
    [event] = events
    assert event["call_id"].startswith("direct-") and event["attempt"] == 1
    assert (event["consumer"], event["agent"], event["session"]) == ("c", "loop", "k")
    _validator("usage-record").validate(event)


@pytest.mark.parametrize("budgeted", [False, True])
async def test_agent_loop_calls_are_attributed(make_kernel, sink, budgeted) -> None:
    """Calls the agent loop makes outside the LLM layer carry consumer, agent, session,
    a call id and an attempt (spec 001 FR-009), with or without a budget."""
    from moeka.agents import AgentSpec

    kernel, _ = make_kernel(**({"budget": CapBudget(limit_usd=5)} if budgeted else {}))
    result = await kernel.agent(AgentSpec(name="scribe")).run("hello", session="sess-1")
    assert result.stop_reason == "completed"
    calls = sink.named("model.call")
    assert calls
    for event in calls:
        assert (event["consumer"], event["agent"], event["session"]) == (
            "test-app", "scribe", "sess-1")
        assert event["call_id"] and event["attempt"] >= 1
        _validator("usage-record").validate(event)
    assert kernel.usage.total(agent="scribe", session="sess-1").requests == len(calls)


# -- store and queries ---------------------------------------------------------------------


async def test_query_by_consumer_agent_session_and_totals(make_kernel) -> None:
    kernel, _ = make_kernel()
    await kernel.llm.generate([user("1")], _opts(agent="a1", session="s1"))
    await kernel.llm.generate([user("2")], _opts(agent="a1", session="s1"))
    await kernel.llm.generate([user("3")], _opts(agent="a2", session="s2"))
    usage = kernel.usage
    assert usage.total(consumer="test-app").requests == 3
    assert usage.total(consumer="other").requests == 0
    assert usage.total(agent="a1").tokens_in == 40
    assert usage.total(session="s2").tokens_out == 5
    by_agent = {t.group["agent"]: t for t in usage.totals(["agent"], consumer="test-app")}
    assert by_agent["a1"].requests == 2 and by_agent["a2"].requests == 1
    assert by_agent["a1"].cost_usd == pytest.approx(2 * (20 * 1.0 + 5 * 2.0) / 1e6)
    docs = usage.records(session="s1")
    assert len(docs) == 2
    for doc in docs:
        _validator("usage-record").validate(doc)
        assert doc["consumer"] == "test-app" and doc["agent"] == "a1"


async def test_time_range_filter(make_kernel) -> None:
    kernel, _ = make_kernel()
    await kernel.llm.generate([user("1")])
    [doc] = kernel.usage.records()
    start = doc["started_at_ms"]
    assert kernel.usage.total(since_ms=start, until_ms=start + 1).requests == 1
    assert kernel.usage.total(until_ms=start).requests == 0


async def test_old_rows_without_attribution_migrate(tmp_path) -> None:
    """A v2 database (no attribution columns) opens and reads back as 'unattributed'."""
    import sqlite3
    import time
    from contextlib import closing

    from nanobot.llm_usage.query import records
    from nanobot.llm_usage.store import LLMUsageStore

    path = tmp_path / "llm_usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, started_at_ms INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
              source TEXT NOT NULL, stream INTEGER NOT NULL, finish_reason TEXT NOT NULL,
              input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
              cache_read_tokens INTEGER, cache_write_tokens INTEGER, reported_tokens INTEGER,
              estimated_tokens INTEGER, generation_ms INTEGER, measured_output_tokens INTEGER,
              ttft_ms INTEGER, timed_requests INTEGER, error_status_code INTEGER,
              error_kind TEXT, tier TEXT, cost_usd REAL);
            PRAGMA user_version = 2;
        """)
        connection.execute(
            "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, "
            "stream, finish_reason, input_tokens, output_tokens, total_tokens, cost_usd) "
            "VALUES (?, 5, 'p', 'm', 'user', 0, 'stop', 7, 3, 10, 0.5)",
            (int(time.time() * 1000),),
        )
    store = LLMUsageStore(path)
    [doc] = records(store)
    assert doc["consumer"] == "unattributed" and doc["tokens_in"] == 7
    _validator("usage-record").validate(doc)
    store.close()


# -- cache hits ------------------------------------------------------------------------------


async def test_cache_hit_is_accounted_with_saved_tokens(make_kernel, sink) -> None:
    kernel, fake = make_kernel(cache=DictCache())
    opts = _opts(agent="a", role="r")
    first = await kernel.llm.generate([user("same")], opts)
    second = await kernel.llm.generate([user("same")], opts)
    assert second.cached and len(fake.calls) == 1
    [hit] = sink.named("cache.hit")
    assert hit["kind"] == "cache_hit" and hit["attempt"] == 0 and hit["cache_hit"] is True
    assert hit["tokens_in"] == 0 and hit["tokens_out"] == 0 and hit["cost_usd"] == 0.0
    assert hit["saved_tokens_in"] == 20 and hit["saved_tokens_out"] == 5
    assert hit["saved_cost_usd"] == pytest.approx(first.cost_usd)
    assert (hit["consumer"], hit["agent"], hit["role"]) == ("test-app", "a", "r")
    _validator("usage-record").validate(hit)

    total = kernel.usage.total(consumer="test-app")
    assert total.requests == 1 and total.tokens_in == 20  # billed: the miss only
    assert total.cache_hits == 1 and total.saved_tokens_in == 20 and total.saved_tokens_out == 5
    assert total.saved_cost_usd == pytest.approx(first.cost_usd)
    kinds = sorted(d["kind"] for d in kernel.usage.records())
    assert kinds == ["cache_hit", "model_call"]
    for doc in kernel.usage.records():
        _validator("usage-record").validate(doc)


async def test_prompt_version_changes_the_cache_key(make_kernel) -> None:
    kernel, fake = make_kernel(cache=DictCache())
    for version in ("v1", "v1", "v2", None, None):
        await kernel.llm.generate([user("x")], GenerateOptions(prompt_version=version))
    assert len(fake.calls) == 3  # v1, v2, None; repeats hit the cache
    assert any(d["prompt_version"] == "v2" for d in kernel.usage.records())


# -- waste -----------------------------------------------------------------------------------


async def test_retry_attempt_is_labelled_and_summed(make_kernel, sink) -> None:
    """The attempt a retry superseded is the waste (a linked ``call.waste`` event: the
    attempt's own event is already emitted when the retry starts)."""
    kernel, _ = make_kernel("not json", '{"a": 1}')
    await kernel.llm.complete_json("give json", schema={"type": "object"}, retries=1)
    first, second = sink.named("model.call")
    assert first["attempt"] == 1 and first["waste_label"] is None
    assert second["attempt"] == 2 and second["waste_label"] is None
    [waste] = sink.named("call.waste")
    assert (waste["call_id"], waste["attempt"], waste["waste_label"], waste["waste_set_by"]) == (
        first["call_id"], 1, "retry", "kernel")
    total = kernel.usage.total()
    assert total.wasted_tokens == first["tokens_in"] + first["tokens_out"] > 0
    by = {t.group["waste_label"]: t for t in kernel.usage.totals(["waste_label"])}
    assert by["retry"].requests == 1 and by[None].requests == 1


async def test_discarded_draft_label_is_a_linked_event(make_kernel, sink) -> None:
    kernel, _ = make_kernel()
    done = await kernel.llm.generate([user("draft")])
    assert kernel.usage.total().wasted_tokens == 0
    kernel.usage.label_waste(done.call_id, "discarded_draft")
    total = kernel.usage.total()
    assert total.wasted_tokens == 25 and total.wasted_cost_usd == pytest.approx(done.cost_usd)
    [doc] = kernel.usage.records()
    assert doc["waste_label"] == "discarded_draft" and doc["waste_set_by"] == "caller"
    # the original event was never mutated
    assert sink.named("model.call")[0]["waste_label"] is None
    with pytest.raises(ValueError):
        kernel.usage.label_waste(done.call_id, "bogus")


# -- budget --------------------------------------------------------------------------------------


async def test_budget_snapshot_and_refusal_event(make_kernel, sink) -> None:
    kernel, _ = make_kernel(budget=CapBudget(limit_usd=0.001, limit_tokens=100000))
    snap = kernel.usage.budget()
    assert snap["cap_usd"] == 0.001 and snap["remaining_usd"] == pytest.approx(0.001)
    await kernel.llm.generate([user("hi")], _opts(agent="a", session="s"))
    [admit] = sink.named("budget.admit")
    assert admit["kind"] == "admit" and admit["consumer"] == "test-app"
    _validator("budget-event").validate(admit)
    after = kernel.usage.budget()
    assert after["spent_usd"] == pytest.approx((20 * 1.0 + 5 * 2.0) / 1e6)
    assert after["remaining_usd"] == pytest.approx(0.001 - after["spent_usd"])
    assert after["remaining_tokens"] == 100000 - 25

    with pytest.raises(BudgetExceeded):
        await kernel.llm.generate([user("x " * 20000)], _opts(agent="a", session="s", role="r"))
    [refuse] = sink.named("budget.refuse")
    assert refuse["kind"] == "refuse" and refuse["refusal"]["code"] in ("cap_usd", "cap_tokens")
    assert (refuse["consumer"], refuse["agent"], refuse["session"], refuse["role"]) == (
        "test-app", "a", "s", "r")
    assert refuse["remaining_usd"] is not None
    _validator("budget-event").validate(refuse)
    # the refusal is queryable with the same attribution
    assert kernel.usage.total(agent="a").refusals == 1


async def test_per_tag_refusal_names_its_scope(make_kernel, sink) -> None:
    kernel, _ = make_kernel(budget=CapBudget(per_tag={"stage": 1e-9}))
    with pytest.raises(BudgetExceeded):
        await kernel.llm.generate([user("hi")], GenerateOptions(tags={"stage": "ingest"}))
    [refuse] = sink.named("budget.refuse")
    assert refuse["refusal"]["code"] == "cap_tag"
    assert refuse["scope"] == {"kind": "tag", "key": "stage", "value": "ingest"}
    _validator("budget-event").validate(refuse)


# -- agreement: stream totals equal stored totals --------------------------------------------------


async def test_stream_totals_equal_stored_totals(make_kernel) -> None:
    kernel, _ = make_kernel(cache=DictCache(), budget=CapBudget(limit_usd=10))
    seen: list[dict[str, Any]] = []
    sub = kernel.usage.subscribe(seen.append)
    rng = random.Random(7)
    consumers = ["x", "y"]
    agents = ["a1", "a2", "a3"]
    for i in range(30):
        who = Attribution(
            consumer=rng.choice(consumers), agent=rng.choice(agents),
            session=f"s{rng.randrange(4)}", role=rng.choice(["maker", "checker"]),
        )
        await kernel.llm.generate([user(f"q{i % 12}")], GenerateOptions(attribution=who))
    assert sub.flush() and sub.dropped == 0

    def from_stream(**flt: str) -> dict[str, float]:
        out = {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "hits": 0,
               "saved_in": 0}
        for ev in seen:
            if any(ev.get(k) != v for k, v in flt.items()):
                continue
            if ev["event"] == "model.call":
                out["requests"] += 1
                out["tokens_in"] += ev["tokens_in"]
                out["tokens_out"] += ev["tokens_out"]
                out["cost"] += ev["cost_usd"] or 0.0
            elif ev["event"] == "cache.hit":
                out["hits"] += 1
                out["saved_in"] += ev["saved_tokens_in"]
        return out

    for _ in range(100):
        flt = {}
        for key, pool in (("consumer", consumers), ("agent", agents),
                          ("session", [f"s{n}" for n in range(4)]),
                          ("role", ["maker", "checker"])):
            if rng.random() < 0.4:
                flt[key] = rng.choice(pool)
        stored = kernel.usage.total(**flt)
        stream = from_stream(**flt)
        assert stored.requests == stream["requests"], flt
        assert stored.tokens_in == stream["tokens_in"], flt
        assert stored.tokens_out == stream["tokens_out"], flt
        assert stored.cost_usd == pytest.approx(stream["cost"]), flt
        assert stored.cache_hits == stream["hits"], flt
        assert stored.saved_tokens_in == stream["saved_in"], flt
    assert from_stream()["hits"] > 0 and from_stream()["requests"] > 0
    for ev in seen:
        _validator("budget-event" if ev["event"].startswith("budget") else "usage-record").validate(ev)


def test_usage_sink_figures_agree_with_the_event() -> None:
    """The legacy per-turn payload and the model.call event give equal token figures,
    including cache writes (spec 001 FR-014)."""
    usage = LLMUsage.reported(
        input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=25,
    )
    legacy = usage.to_turn_dict()
    record = LLMCallRecord(
        started_at_ms=1, duration_ms=1, provider="p", model="m", source="user",
        stream=False, finish_reason="stop", usage=usage,
    )
    event = LedgerObserver().event_for(record).to_trace()
    assert event["tokens_in"] == legacy["prompt_tokens"]
    assert event["tokens_out"] == legacy["completion_tokens"]
    assert event["tokens_cache_read"] == legacy["cached_tokens"]
    assert event["tokens_cache_write"] == legacy["cache_write_tokens"]


# -- fail-open observation -----------------------------------------------------------------------------


async def test_blocked_and_raising_subscribers_never_delay_calls(make_kernel) -> None:
    kernel, _ = make_kernel()
    gate = threading.Event()
    blocked = kernel.usage.subscribe(lambda e: gate.wait(10), max_queue=3)

    def boom(event: dict[str, Any]) -> None:
        raise RuntimeError("subscriber down")

    raising = kernel.usage.subscribe(boom)
    try:
        for i in range(40):
            done = await kernel.llm.generate([user(f"q{i}")])
            assert done.text == "ok"
        assert raising.flush()
        assert blocked.dropped > 0
        assert raising.errors == 40
        loss = kernel.usage.loss()
        assert loss["dropped_events"] == blocked.dropped and loss["subscriber_errors"] == 40
        assert kernel.usage.total().requests == 40
    finally:
        gate.set()


async def test_failing_store_never_fails_calls_and_is_counted(make_kernel, monkeypatch) -> None:
    from nanobot.llm_usage.store import LLMUsageStore

    kernel, _ = make_kernel(cache=DictCache())

    def broken(self: Any, event: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(LLMUsageStore, "record_usage_event", broken)
    await kernel.llm.generate([user("a")])
    await kernel.llm.generate([user("a")])  # a hit: its store write fails
    assert kernel.usage.loss()["store_failures"] == 1


def test_schema_versions_are_reported(make_kernel) -> None:
    kernel, _ = make_kernel()
    assert kernel.usage.schema_version == SCHEMA_VERSION == "1.1"
    assert SCHEMA_VERSION in kernel.usage.schema_versions()
    assert UsageFilter(consumer="x").consumer == "x"
