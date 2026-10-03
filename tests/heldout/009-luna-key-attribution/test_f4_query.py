"""FR-004 / SC-001: group totals by ``key_fp`` and filter by it."""

from __future__ import annotations

import dataclasses

import pytest
from _h009 import KEY_AWORK, KEY_MOEKA, KEY_THIRD, keyed_provider, one_call, record, ref_fp

from nanobot.llm_usage import query
from nanobot.llm_usage.query import GROUP_BY_CALLS, UsageFilter

FP1, FP2, FP3 = ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK), ref_fp(KEY_THIRD)


def _seed(store):
    """fp1: 3 calls 0.10+0.20+0.30; fp2: 2 calls 0.25+0.05; none: 1 call 0.40; fp3: unpriced."""
    rows = [
        (FP1, 0.10, 100, 10), (FP1, 0.20, 200, 20), (FP1, 0.30, 300, 30),
        (FP2, 0.25, 1000, 100), (FP2, 0.05, 50, 5),
        (None, 0.40, 7, 3),
        (FP3, None, 11, 1),
    ]
    for i, (fp, cost, tin, tout) in enumerate(rows):
        store.record(record(key_fp=fp, cost_usd=cost, tin=tin, tout=tout,
                            started_at_ms=record().started_at_ms + i * 1000))


@pytest.mark.fr("FR-004")
def test_key_fp_is_groupable():
    assert "key_fp" in GROUP_BY_CALLS
    for name in ("consumer", "model", "provider", "tier", "source"):
        assert name in GROUP_BY_CALLS  # nothing removed


@pytest.mark.fr("FR-004", "SC-001")
def test_group_by_key_fp_splits_exactly(store):
    _seed(store)
    by = {t.group["key_fp"]: t for t in query.totals(store, group_by=("key_fp",))}
    assert set(by) == {FP1, FP2, FP3, None}
    assert by[FP1].requests == 3 and by[FP1].cost_usd == pytest.approx(0.60, abs=1e-9)
    assert by[FP1].tokens_in == 600 and by[FP1].tokens_out == 60
    assert by[FP2].requests == 2 and by[FP2].cost_usd == pytest.approx(0.30, abs=1e-9)
    assert by[FP2].tokens_in == 1050 and by[FP2].tokens_out == 105
    assert by[None].requests == 1 and by[None].cost_usd == pytest.approx(0.40, abs=1e-9)
    assert by[FP3].requests == 1 and by[FP3].unpriced_requests == 1
    total = query.totals(store)[0]
    assert sum(t.requests for t in by.values()) == total.requests == 7
    assert sum(t.cost_usd for t in by.values()) == pytest.approx(total.cost_usd, abs=1e-9)
    assert sum(t.tokens_in for t in by.values()) == total.tokens_in


@pytest.mark.fr("FR-004")
def test_group_by_key_fp_and_model(store):
    store.record(record(key_fp=FP1, model="m-a", cost_usd=0.1))
    store.record(record(key_fp=FP1, model="m-b", cost_usd=0.2))
    store.record(record(key_fp=FP2, model="m-a", cost_usd=0.4))
    got = {(t.group["key_fp"], t.group["model"]): t.requests
           for t in query.totals(store, group_by=("key_fp", "model"))}
    assert got == {(FP1, "m-a"): 1, (FP1, "m-b"): 1, (FP2, "m-a"): 1}


@pytest.mark.fr("FR-004")
def test_filter_field_default_none():
    names = {f.name: f for f in dataclasses.fields(UsageFilter)}
    assert "key_fp" in names and names["key_fp"].default is None
    assert UsageFilter().key_fp is None
    assert UsageFilter(key_fp=FP1).key_fp == FP1


@pytest.mark.fr("FR-004")
def test_filter_restricts_totals(store):
    _seed(store)
    t1 = query.totals(store, UsageFilter(key_fp=FP1))
    assert len(t1) == 1
    assert t1[0].requests == 3 and t1[0].cost_usd == pytest.approx(0.60, abs=1e-9)
    t2 = query.totals(store, UsageFilter(key_fp=FP2))
    assert t2[0].requests == 2 and t2[0].cost_usd == pytest.approx(0.30, abs=1e-9)
    assert query.totals(store, UsageFilter())[0].requests == 7


@pytest.mark.fr("FR-004")
def test_filter_unknown_fp_matches_nothing(store):
    _seed(store)
    t = query.totals(store, UsageFilter(key_fp="0123456789ab"))
    assert sum(x.requests for x in t) == 0
    assert [r for r in query.records(store, UsageFilter(key_fp="0123456789ab"))
            if r["kind"] == "model_call"] == []


@pytest.mark.fr("FR-004", "FR-005")
def test_filter_restricts_records(store):
    _seed(store)
    rows = [r for r in query.records(store, UsageFilter(key_fp=FP2), limit=50)
            if r["kind"] == "model_call"]
    assert len(rows) == 2 and {r["key_fp"] for r in rows} == {FP2}


@pytest.mark.fr("FR-004")
def test_filter_combines_with_other_fields(store):
    store.record(record(key_fp=FP1, model="m-a"))
    store.record(record(key_fp=FP1, model="m-b"))
    store.record(record(key_fp=FP2, model="m-a"))
    t = query.totals(store, UsageFilter(key_fp=FP1, model="m-a"))
    assert t[0].requests == 1
    t = query.totals(store, UsageFilter(key_fp=FP1), group_by=("model",))
    assert {x.group["model"]: x.requests for x in t} == {"m-a": 1, "m-b": 1}


def _cache_hit(store, call_id="hit-1"):
    store.record_usage_event({
        "event": "cache.hit", "call_id": call_id, "consumer": "moeka",
        "model": "openai/gpt-6-luna", "provider": "openrouter",
        "saved_tokens_in": 10, "saved_tokens_out": 2, "saved_cost_usd": 0.01,
        "started_at_ms": record().started_at_ms,
    })


@pytest.mark.fr("FR-004")
def test_events_do_not_break_group_by_key_fp(store):
    _seed(store)
    _cache_hit(store)
    by = {t.group["key_fp"]: t for t in query.totals(store, group_by=("key_fp",))}
    assert by[FP1].requests == 3 and by[FP2].requests == 2
    assert by[FP1].cache_hits == 0 and by[FP2].cache_hits == 0  # events carry no key
    assert set(by) <= {FP1, FP2, FP3, None}


@pytest.mark.fr("FR-004")
def test_events_do_not_break_key_fp_filter(store):
    _seed(store)
    _cache_hit(store)
    t = query.totals(store, UsageFilter(key_fp=FP1))
    assert sum(x.requests for x in t) == 3
    rows = query.records(store, UsageFilter(key_fp=FP1), limit=50)
    assert [r["key_fp"] for r in rows if r["kind"] == "model_call"] == [FP1] * 3


@pytest.mark.fr("FR-004", "FR-005")
def test_reference_reducer_agrees_with_store_totals(store):
    _seed(store)
    docs = query.records(store, limit=100)
    ref = {t.group["key_fp"]: (t.requests, round(t.cost_usd, 9))
           for t in query.reduce_records(docs, group_by=("key_fp",))}
    got = {t.group["key_fp"]: (t.requests, round(t.cost_usd, 9))
           for t in query.totals(store, group_by=("key_fp",))}
    assert ref == got


@pytest.mark.fr("SC-001", "FR-001", "FR-002", "FR-003", "FR-004")
async def test_sc001_two_providers_two_keys_end_to_end(store):
    """Two providers, two keys, through the ledger into the store; totals split exactly."""
    from nanobot.kernel.ledger import LedgerObserver, ModelPricing

    pricing = {("openrouter", "openai/gpt-6-luna"): ModelPricing("fast", 0.10, 0.50, None)}
    moeka = keyed_provider(KEY_MOEKA, tin=1_000_000, tout=1_000_000)
    awork = keyed_provider(KEY_AWORK, tin=500_000, tout=200_000)
    for p in (moeka, awork):
        p.set_llm_call_observer(LedgerObserver(store=store, pricing=pricing))
    await one_call(moeka)
    await one_call(moeka)
    await one_call(awork)
    rows = [r for r in query.records(store, limit=50) if r["kind"] == "model_call"]
    fps = sorted(r["key_fp"] for r in rows)
    assert fps == sorted([FP1, FP1, FP2])
    assert FP1 != FP2
    by = {t.group["key_fp"]: t for t in query.totals(store, group_by=("key_fp",))}
    assert set(by) == {FP1, FP2}
    assert by[FP1].requests == 2 and by[FP2].requests == 1
    assert by[FP1].cost_usd == pytest.approx(1.20, abs=1e-9)
    assert by[FP2].cost_usd == pytest.approx(0.05 + 0.10, abs=1e-9)
    assert by[FP1].tokens_in == 2_000_000 and by[FP2].tokens_out == 200_000
