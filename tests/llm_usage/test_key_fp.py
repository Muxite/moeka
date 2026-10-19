"""Spec 009: the usage store persists ``key_fp``; the read side filters, groups and exports it."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from nanobot.kernel import ledger
from nanobot.llm_usage import query
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.query import GROUP_BY_CALLS, SCHEMA_VERSION, UsageFilter, records, totals
from nanobot.llm_usage.store import LLMUsageStore
from nanobot.providers.base import LLMUsage
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
KEY_A = "sk-or-v1-aaaaFAKEaaaa0000111122223333444455556666"
KEY_B = "sk-or-v1-bbbbFAKEbbbb7777888899990000aaaabbbbcccc"
_NOW_MS = int(time.time() * 1000)  # inside the store's retention window


def _fp(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _call(key_fp: str | None, *, cost: float | None = 0.25, at: int = 0) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=_NOW_MS + at, duration_ms=10, provider="openrouter",
        model="openai/gpt-6-luna", source="api", stream=False, finish_reason="stop",
        usage=LLMUsage.reported(input_tokens=100, output_tokens=10), cost_usd=cost,
        key_fp=key_fp,
    )


@pytest.fixture
def store(tmp_path: Path):
    s = LLMUsageStore(tmp_path / "llm_usage.sqlite3")
    yield s
    s.close()


def test_record_writes_key_fp(store: LLMUsageStore) -> None:
    store.record(_call(_fp(KEY_A)))
    store.record(_call(None, at=1))
    with closing(sqlite3.connect(store.path)) as connection:
        got = [r[0] for r in connection.execute("SELECT key_fp FROM llm_calls ORDER BY id")]
    assert got == [_fp(KEY_A), None]


def test_two_keys_split_totals_exactly(store: LLMUsageStore) -> None:
    fp_a = OpenAICompatProvider(api_key=KEY_A, provider_name="openrouter").key_fp
    fp_b = OpenAICompatProvider(api_key=KEY_B, provider_name="openrouter").key_fp
    assert fp_a != fp_b
    for i in range(3):
        store.record(_call(fp_a, cost=0.5, at=i))
    store.record(_call(fp_b, cost=0.125, at=10))
    store.record(_call(None, cost=None, at=20))
    assert "key_fp" in GROUP_BY_CALLS
    by_key = {t.group["key_fp"]: t for t in totals(store, group_by=("key_fp",))}
    assert set(by_key) == {fp_a, fp_b, None}
    assert (by_key[fp_a].requests, by_key[fp_a].cost_usd) == (3, pytest.approx(1.5))
    assert (by_key[fp_b].requests, by_key[fp_b].cost_usd) == (1, pytest.approx(0.125))
    assert (by_key[None].requests, by_key[None].unpriced_requests) == (1, 1)

    [only_a] = totals(store, UsageFilter(key_fp=fp_a))
    assert only_a.requests == 3 and only_a.cost_usd == pytest.approx(1.5)
    assert UsageFilter().key_fp is None


def test_key_fp_filter_excludes_events(store: LLMUsageStore) -> None:
    store.record(_call(_fp(KEY_A)))
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "INSERT INTO llm_usage_events (ts_ms, kind, model, provider, saved_tokens_in) "
            "VALUES (?, 'cache_hit', 'm', 'openrouter', 5)", (_NOW_MS,),
        )
    [unfiltered] = totals(store)
    assert unfiltered.cache_hits == 1
    [filtered] = totals(store, UsageFilter(key_fp=_fp(KEY_A)))
    assert (filtered.requests, filtered.cache_hits) == (1, 0)
    grouped = {t.group["key_fp"]: t for t in totals(store, group_by=("key_fp",))}
    assert grouped[None].cache_hits == 1 and grouped[_fp(KEY_A)].cache_hits == 0
    docs = records(store, UsageFilter(key_fp=_fp(KEY_A)))
    assert [d["kind"] for d in docs] == ["model_call"]


def test_export_rows_carry_key_fp_and_validate(store: LLMUsageStore) -> None:
    sys.path.insert(0, str(SCHEMAS))
    try:
        import validate
    finally:
        sys.path.remove(str(SCHEMAS))
    store.record(_call(_fp(KEY_A)))
    store.record(_call(None, at=1))
    docs = records(store)
    assert SCHEMA_VERSION == "1.3" == ledger.SCHEMA_VERSION
    assert {d["schema_version"] for d in docs} == {"1.3"}
    assert sorted((d["key_fp"] or "") for d in docs) == ["", _fp(KEY_A)]
    for doc in docs:
        assert validate.problems(doc, "usage-record") == []
    bad = dict(docs[0], key_fp="NOT-A-FP")
    assert validate.problems(bad, "usage-record")
    reduced = {t.group["key_fp"]: t.requests for t in query.reduce_records(docs, ("key_fp",))}
    assert reduced == {_fp(KEY_A): 1, None: 1}


def test_a_1_1_document_without_key_fp_stays_valid() -> None:
    sys.path.insert(0, str(SCHEMAS))
    try:
        import validate
    finally:
        sys.path.remove(str(SCHEMAS))
    doc = json.loads((SCHEMAS / "examples" / "usage-record.model-call.json").read_text())
    doc.pop("key_fp", None)
    doc["schema_version"] = "1.1"
    assert validate.problems(doc, "usage-record") == []


# -- migration (FR-003, NFR-002, SC-003) -------------------------------------------------

_V4_NO_KEY_FP = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY, started_at_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, source TEXT NOT NULL, stream INTEGER NOT NULL,
    finish_reason TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
    total_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    reported_tokens INTEGER, estimated_tokens INTEGER, generation_ms INTEGER,
    measured_output_tokens INTEGER, ttft_ms INTEGER, timed_requests INTEGER,
    error_status_code INTEGER, error_kind TEXT, tier TEXT, cost_usd REAL, call_id TEXT,
    attempt INTEGER, consumer TEXT, agent TEXT, session TEXT, role TEXT, purpose TEXT,
    trace_id TEXT, slot TEXT, alias TEXT, request_key TEXT, prompt_version TEXT,
    waste_label TEXT, waste_set_by TEXT, outcome TEXT, price_source TEXT, usage_source TEXT,
    cost_billed INTEGER, tokens_reasoning INTEGER, tags TEXT
);
PRAGMA user_version = 4;
"""


def _columns(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [str(r[1]) for r in connection.execute("PRAGMA table_info(llm_calls)")]


def test_old_store_migrates_and_old_rows_read_none(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_V4_NO_KEY_FP)
        connection.execute(
            "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, stream, "
            "finish_reason, input_tokens, output_tokens, total_tokens, cost_usd) "
            "VALUES (?, 5, 'openrouter', 'old', 'user', 0, 'stop', 7, 3, 10, 0.1)", (_NOW_MS,),
        )
    assert "key_fp" not in _columns(path)

    store = LLMUsageStore(path)
    store.record(_call(_fp(KEY_B), at=1))
    by_model = {d["model"]: d for d in records(store)}
    by_key = {t.group["key_fp"]: t.requests for t in totals(store, group_by=("key_fp",))}
    store.close()
    assert by_model["old"]["key_fp"] is None
    assert by_model["openai/gpt-6-luna"]["key_fp"] == _fp(KEY_B)
    assert by_key == {None: 1, _fp(KEY_B): 1}

    after_first = _columns(path)
    assert after_first.count("key_fp") == 1
    LLMUsageStore(path).close()  # a second open adds nothing
    assert _columns(path) == after_first


# -- FR-006 ------------------------------------------------------------------------------


async def test_no_stored_or_traced_byte_contains_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from loguru import logger

    from nanobot.kernel.ledger import LedgerObserver
    from nanobot.providers.base import LLMProvider, LLMResponse

    class _Provider(LLMProvider):
        _CHAT_RETRY_DELAYS = ()

        async def chat(self, **_kwargs: object) -> LLMResponse:
            return LLMResponse(content="ok", usage=LLMUsage.reported(input_tokens=3, output_tokens=1))

        def get_default_model(self) -> str:
            return "openai/gpt-6-luna"

    traced: list[dict] = []
    logs: list[str] = []
    sink_id = logger.add(logs.append, level="DEBUG")

    class _Sink:
        def emit(self, event: dict) -> None:
            traced.append(event)

    store = LLMUsageStore(tmp_path / "llm_usage.sqlite3")
    try:
        provider = _Provider(api_key=KEY_A, provider_name="openrouter")
        provider.set_llm_call_observer(LedgerObserver(sink=_Sink(), store=store))
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
        exported = json.dumps(records(store), default=str)
    finally:
        store.close()
        logger.remove(sink_id)
    assert traced and traced[0]["key_fp"] == _fp(KEY_A)
    blobs = [exported, json.dumps(traced, default=str), "".join(logs), caplog.text]
    blobs += [p.read_bytes().decode("latin-1") for p in tmp_path.iterdir() if p.is_file()]
    for i in range(len(KEY_A) - 7):
        piece = KEY_A[i:i + 8]
        assert not any(piece in blob for blob in blobs), piece
