"""Adversarial review of the usage surface (docs/reviews/2026-10-01-usage-schema-review.md).

Each test states a property a consumer relies on and tries to break it. They were written
failing against ``feat/usage-surface`` (the finding number is in the test name) and pass on
``review/usage``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from moeka.budget import CapBudget
from moeka.llm import GenerateOptions, user
from moeka.testing import FakeProvider, error, reply
from moeka.usage import UNATTRIBUTED, Attribution, bind_attribution
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.ledger import CallAttribution, LedgerObserver, call_attribution
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import LLMUsageStore
from nanobot.providers.base import LLMUsage

jsonschema = pytest.importorskip("jsonschema")
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"

MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)


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
        self.data: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def put(self, key: str, completion: Any) -> None:
        self.data[key] = completion


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(*script: Any, default: Any = None, **kwargs: Any) -> tuple[Kernel, FakeProvider]:
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
        fake = FakeProvider(
            script, default=default or reply("ok", input_tokens=20, output_tokens=5),
        )
        kernel.llm.register_provider("main", fake, MAIN)
        return kernel, fake

    yield make
    for kernel in kernels:
        kernel.close()


def _event_of(observer: LedgerObserver, rec: LLMCallRecord) -> dict[str, Any]:
    with call_attribution(CallAttribution(call_id="c-1", attempts=1)):
        return observer.event_for(rec).to_trace()


def _record(**kw: Any) -> LLMCallRecord:
    base: dict[str, Any] = {
        "started_at_ms": 1_790_000_000_000, "duration_ms": 5, "provider": "p", "model": "m",
        "source": "user", "stream": False, "finish_reason": "stop",
        "usage": LLMUsage.reported(input_tokens=10, output_tokens=4),
    }
    base.update(kw)
    return LLMCallRecord(**base)


# -- R1 waste labels ---------------------------------------------------------------------------


async def test_r1_retry_waste_is_the_superseded_attempt(make_kernel, sink) -> None:
    """The attempt that did NOT contribute (attempt 1, malformed) is the waste; the retry
    that produced the answer is not. 1.0 labelled it the other way round."""
    kernel, _ = make_kernel("not json", '{"a": 1}')
    await kernel.llm.complete_json("give json", schema={"type": "object"}, retries=1)
    first, second = sink.named("model.call")
    assert second["waste_label"] is None
    total = kernel.usage.total()
    assert total.wasted_tokens == first["tokens_in"] + first["tokens_out"]
    docs = {d["attempt"]: d for d in kernel.usage.records()}
    assert docs[1]["waste_label"] == "retry" and docs[1]["waste_set_by"] == "kernel"
    assert docs[2]["waste_label"] is None


async def test_r1_late_label_targets_its_own_attempt(make_kernel, sink) -> None:
    """``label_waste(call, label, attempt=3)`` must not relabel attempts 1 and 2."""
    kernel, _ = make_kernel("bad", "worse", '{"a": 1}')
    await kernel.llm.complete_json("give json", schema={"type": "object"}, retries=2)
    call_id = sink.named("model.call")[0]["call_id"]
    kernel.usage.label_waste(call_id, "discarded_draft", attempt=3)
    docs = {d["attempt"]: d for d in kernel.usage.records()}
    assert docs[1]["waste_label"] == docs[2]["waste_label"] == "retry"
    assert docs[3]["waste_label"] == "discarded_draft"
    by = {t.group["waste_label"]: t for t in kernel.usage.totals(["waste_label"])}
    assert by["retry"].requests == 2 and by["discarded_draft"].requests == 1


# -- R2 idempotency ----------------------------------------------------------------------------


def test_r2_duplicate_delivery_is_idempotent(tmp_path) -> None:
    """The same attempt delivered twice (a replayed trace, a retried write) counts once."""
    store = LLMUsageStore(tmp_path / "u.sqlite3")
    observer = LedgerObserver(store=store)
    rec = _record()
    with call_attribution(CallAttribution(call_id="c-1", attempts=1)):
        event = observer.event_for(rec)
    store.record_event(rec, event)
    store.record_event(rec, event)
    assert store.count() == 1
    assert store.stats()["duplicates"] == 1


async def test_r2_duplicate_cache_hit_and_waste_events_count_once(make_kernel, sink) -> None:
    kernel, _ = make_kernel(cache=DictCache())
    await kernel.llm.generate([user("a")])
    await kernel.llm.generate([user("a")])
    [hit] = sink.named("cache.hit")
    store = kernel.usage._store()  # noqa: SLF001
    store.record_usage_event(hit)  # the same event again
    assert kernel.usage.total().cache_hits == 1


# -- R3 null versus zero -------------------------------------------------------------------------


def test_r3_unreported_cache_tokens_stay_null_on_the_event() -> None:
    usage = LLMUsage.reported(input_tokens=10, output_tokens=4)  # cache counts not reported
    event = _event_of(LedgerObserver(), _record(usage=usage))
    assert event["tokens_cache_read"] is None and event["tokens_cache_write"] is None
    zero = LLMUsage.reported(input_tokens=10, output_tokens=4, cache_read_tokens=0)
    assert _event_of(LedgerObserver(), _record(usage=zero))["tokens_cache_read"] == 0
    _validator("usage-record").validate(event)


async def test_r3_stream_and_store_agree_on_null(make_kernel, sink) -> None:
    kernel, _ = make_kernel()
    await kernel.llm.generate([user("a")])
    [ev] = sink.named("model.call")
    [doc] = kernel.usage.records()
    assert ev["tokens_cache_read"] == doc["tokens_cache_read"]


# -- R4 hidden tokens ----------------------------------------------------------------------------


def test_r4_hidden_output_tokens_are_billed_and_visible() -> None:
    """A provider total above input + output is hidden (reasoning) output. It is billed, so it
    belongs in tokens_out (and cost) and is exposed in tokens_reasoning, a subset of it."""
    usage = LLMUsage.reported(input_tokens=100, output_tokens=40, total_tokens=190)
    observer = LedgerObserver(pricing={("p", "m"): _pricing(1.0, 2.0)})
    event = _event_of(observer, _record(usage=usage))
    assert event["tokens_out"] == 90 and event["tokens_reasoning"] == 50
    assert event["tokens_in"] + event["tokens_out"] == 190
    assert event["cost_usd"] == pytest.approx((100 * 1.0 + 90 * 2.0) / 1e6)
    _validator("usage-record").validate(event)


def _pricing(price_in: float, price_out: float) -> Any:
    from nanobot.kernel.ledger import ModelPricing

    return ModelPricing(tier="paid", price_in=price_in, price_out=price_out)


# -- R5 losing events silently -------------------------------------------------------------------


async def test_r5_locked_store_is_counted_and_recovered(make_kernel) -> None:
    kernel, _ = make_kernel()
    await kernel.llm.generate([user("a")])
    db = kernel.core_env.paths.data_dir / "llm_usage.sqlite3"
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        done = await kernel.llm.generate([user("b")])  # the write times out; the call succeeds
        assert done.text == "ok"
    finally:
        blocker.execute("COMMIT")
        blocker.close()
    assert kernel.usage.loss()["store_failures"] >= 1
    await kernel.llm.generate([user("c")])
    # the lost row is retried with the next write: nothing is silently missing
    assert kernel.usage.total().requests == 3
    assert kernel.usage.loss()["pending_writes"] == 0


def test_r5_pending_buffer_is_bounded(tmp_path) -> None:
    store = LLMUsageStore(tmp_path / "u.sqlite3")
    store._connect()  # noqa: SLF001
    store.MAX_PENDING = 5
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        for i in range(store.MAX_PENDING + 20):
            rec = _record(started_at_ms=1_790_000_000_000 + i)
            with pytest.raises(sqlite3.OperationalError):
                store.record(rec)
    finally:
        blocker.execute("COMMIT")
        blocker.close()
    stats = store.stats()
    assert stats["pending_writes"] == store.MAX_PENDING and stats["write_dropped"] == 20


# -- R6 retention is visible -----------------------------------------------------------------------


def test_r6_pruning_is_reported_and_events_are_pruned_too(tmp_path, monkeypatch) -> None:
    import nanobot.llm_usage.store as mod

    monkeypatch.setattr(mod, "MAX_CALLS_RETAINED", 5)
    store = LLMUsageStore(tmp_path / "u.sqlite3")
    for i in range(12):
        store._last_prune_utc_day = None  # noqa: SLF001 - force the due check
        store.record(_record(started_at_ms=1_790_000_000_000 + i))
        store.record_usage_event({
            "event": "cache.hit", "call_id": f"h{i}", "started_at_ms": 1_790_000_000_000 + i,
            "saved_tokens_in": 1, "saved_tokens_out": 1, "saved_cost_usd": 0.0,
        })
    assert store.count() == 5  # the newest MAX_CALLS_RETAINED survive
    stats = store.stats()
    assert stats["pruned_rows"] >= 6
    assert stats["oldest_started_at_ms"] is not None
    n_events = store._connect().execute("SELECT COUNT(*) FROM llm_usage_events").fetchone()[0]  # noqa: SLF001
    assert n_events <= 6


# -- R7 tags: bounded, content-free, round-trip ---------------------------------------------------------


async def test_r7_tags_are_bounded_and_round_trip(make_kernel, sink) -> None:
    kernel, _ = make_kernel(cache=DictCache())
    tags = {"stage": "ingest", "prompt": "x" * 5000, "nested": {"a": [1, 2]}}
    tags.update({f"k{i}": i for i in range(60)})
    opts = GenerateOptions(tags=tags)
    await kernel.llm.generate([user("a")], opts)
    await kernel.llm.generate([user("a")], opts)  # cache hit carries the same bounded tags
    [call] = sink.named("model.call")
    [hit] = sink.named("cache.hit")
    for ev in (call, hit):
        assert len(json.dumps(ev["tags"])) <= 2048
        assert len(ev["tags"]) <= 17 and ev["tags"]["stage"] == "ingest"
        assert "prompt" not in ev["tags"] and "nested" not in ev["tags"]
        assert all(isinstance(v, (str, int, float, bool, type(None))) for v in ev["tags"].values())
        assert ev["tags"].get("moeka.tags_truncated") is True
        _validator("usage-record").validate(ev)
    docs = kernel.usage.records()
    assert {json.dumps(d["tags"], sort_keys=True) for d in docs} == {
        json.dumps(call["tags"], sort_keys=True)}


async def test_r7_oversized_event_does_not_corrupt_the_stored_payload(tmp_path) -> None:
    store = LLMUsageStore(tmp_path / "u.sqlite3")
    store.record_usage_event({
        "event": "cache.hit", "call_id": "h1", "started_at_ms": 5, "request_key": "k" * 64,
        "prompt_version": "v1", "tags": {"a": "x" * 20000}, "saved_tokens_in": 1,
        "saved_tokens_out": 1, "saved_cost_usd": 0.1,
    })
    from nanobot.llm_usage.query import UsageFilter, records

    [doc] = records(store, UsageFilter())
    assert doc["request_key"] == "k" * 64 and doc["prompt_version"] == "v1"


# -- R8 billed versus estimated ---------------------------------------------------------------------


async def test_r8_totals_split_billed_and_estimated_cost(make_kernel) -> None:
    est = reply("ok", usage=LLMUsage.estimated(input_tokens=1000, output_tokens=500))
    kernel, _ = make_kernel(reply("ok", input_tokens=1000, output_tokens=500), est)
    await kernel.llm.generate([user("a")])
    await kernel.llm.generate([user("b")])
    total = kernel.usage.total()
    assert total.estimated_requests == 1
    assert total.billed_cost_usd == pytest.approx(0.002)
    assert total.estimated_cost_usd == pytest.approx(0.002)
    assert total.cost_usd == pytest.approx(total.billed_cost_usd + total.estimated_cost_usd)


# -- R9 cancelled and timed-out calls spend money -------------------------------------------------------


async def test_r9_timed_out_call_still_counts_against_the_cap(make_kernel) -> None:
    """The provider may have billed a call that timed out or was cancelled; the budget must
    not hand the whole reservation back (awork-resume charges it: M7)."""
    budget = CapBudget(limit_usd=1.0)
    kernel, _ = make_kernel(
        error(kind="timeout", should_retry=False), default=error(kind="timeout", should_retry=False),
        budget=budget,
    )
    with pytest.raises(Exception):  # noqa: B017, PT011 - any LLM error
        await kernel.llm.generate([user("a")])
    snap = kernel.usage.budget()
    assert snap["spent_usd"] > 0 and snap["reserved_usd"] == 0


async def test_r9_cancelled_call_still_counts_against_the_cap(make_kernel) -> None:
    budget = CapBudget(limit_usd=1.0)
    kernel, fake = make_kernel(budget=budget)
    fake.delay = 5.0
    task = asyncio.create_task(kernel.llm.generate([user("a")]))
    for _ in range(300):  # wait until the call is in flight at the provider
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    assert fake.calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(300):  # the cancellation lands on the kernel loop a moment later
        snap = kernel.usage.budget()
        if snap["reserved_usd"] == 0:
            break
        await asyncio.sleep(0.01)
    assert snap["spent_usd"] > 0 and snap["reserved_usd"] == 0
    [rec] = kernel.usage.records()
    assert rec["outcome"] == "cancelled" and rec["cost_usd"] is None


async def test_concurrent_calls_never_overshoot_the_cap(make_kernel) -> None:
    budget = CapBudget(limit_usd=0.002)
    kernel, fake = make_kernel(budget=budget)
    fake.delay = 0.05
    results = await asyncio.gather(
        *(kernel.llm.generate([user(f"q{i}")]) for i in range(40)), return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, Exception)]
    assert ok and refused
    snap = kernel.usage.budget()
    assert snap["spent_usd"] <= 0.002 and snap["reserved_usd"] == 0
    total = kernel.usage.total()
    assert total.requests == len(ok) and total.refusals == len(refused)
    assert total.cost_usd == pytest.approx(snap["spent_usd"])


# -- R10 attribution leaks -----------------------------------------------------------------------------------


async def test_r10_provider_call_outside_the_llm_layer_gets_the_kernel_consumer(
    make_kernel, sink,
) -> None:
    """Dream, memory and router calls reach the provider with no bound attribution. The kernel
    still knows who it serves, so they must not fall to ``unattributed``."""
    kernel, fake = make_kernel()
    await fake.chat_with_retry(messages=[{"role": "user", "content": "x"}])
    [ev] = sink.named("model.call")
    assert ev["consumer"] == "test-app"
    assert kernel.usage.total(consumer=UNATTRIBUTED).requests == 0


async def test_r10_plain_thread_loses_ambient_attribution_but_keeps_the_kernel_consumer(
    make_kernel, sink,
) -> None:
    kernel, fake = make_kernel()

    def worker() -> None:
        asyncio.run(fake.chat_with_retry(messages=[{"role": "user", "content": "x"}]))

    with bind_attribution(Attribution(agent="bound")):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    [ev] = sink.named("model.call")
    assert ev["consumer"] == "test-app"  # the kernel default; `agent` is lost, which is documented
    assert ev["agent"] is None


async def test_r10_unattributed_traffic_is_surfaced(make_kernel) -> None:
    kernel, fake = make_kernel(consumer=None)
    await fake.chat_with_retry(messages=[{"role": "user", "content": "x"}])
    assert kernel.usage.loss()["unattributed_requests"] == 1


# -- R11 forging -----------------------------------------------------------------------------------------------


async def test_r11_usage_database_is_in_the_file_floor(make_kernel) -> None:
    from nanobot.security.protected_paths import ProtectedFloor

    kernel, _ = make_kernel()
    await kernel.llm.generate([user("a")])
    paths = kernel.core_env.paths
    floor = ProtectedFloor.from_paths(paths)
    db = paths.data_dir / "llm_usage.sqlite3"
    for name in ("", "-wal", "-shm", "-journal"):
        assert floor.matches(Path(f"{db}{name}"), write=True), name


@pytest.mark.parametrize("cmd", [
    "sqlite3 /x/data/llm_usage.sqlite3 'update llm_calls set cost_usd=0'",
    "python3 -c \"import sqlite3; sqlite3.connect('/x/data/llm_usage.sqlite3')\"",
    "echo > /x/data/llm_usage.sqlite3",
    "cp /tmp/forged.db /x/data/llm_usage.sqlite3",
    "rm -f /x/data/llm_usage.sqlite3-wal",
])
def test_r11_exec_floor_blocks_obvious_writes_to_the_usage_database(cmd: str) -> None:
    from nanobot.agent.tools.shell import ExecTool

    assert ExecTool.floor_denial(cmd.lower()) is not None


async def test_r11_checkpoint_detects_a_rewritten_ledger(make_kernel) -> None:
    kernel, _ = make_kernel()
    for i in range(3):
        await kernel.llm.generate([user(f"q{i}")])
    cp = kernel.usage.checkpoint()  # the harness keeps this outside the agent's reach
    assert kernel.usage.verify(cp)["ok"]
    await kernel.llm.generate([user("later")])  # honest appends do not break it
    assert kernel.usage.verify(cp)["ok"]
    db = kernel.core_env.paths.data_dir / "llm_usage.sqlite3"
    forger = sqlite3.connect(db, isolation_level=None)
    forger.execute("UPDATE llm_calls SET output_tokens = 0, cost_usd = 0 WHERE id = 2")
    forger.close()
    verdict = kernel.usage.verify(cp)
    assert not verdict["ok"] and verdict["reason"] == "rows_changed"


async def test_r11_checkpoint_detects_deleted_rows(make_kernel) -> None:
    kernel, _ = make_kernel()
    for i in range(3):
        await kernel.llm.generate([user(f"q{i}")])
    cp = kernel.usage.checkpoint()
    db = kernel.core_env.paths.data_dir / "llm_usage.sqlite3"
    forger = sqlite3.connect(db, isolation_level=None)
    forger.execute("DELETE FROM llm_calls WHERE id = 3")
    forger.close()
    assert not kernel.usage.verify(cp)["ok"]


# -- R12 migration, old rows, rollback ------------------------------------------------------------------


def _downgrade_to_v3(path: Path) -> None:
    """Make *path* look like a database written by feat/usage-surface (user_version 3)."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("DROP INDEX IF EXISTS llm_calls_attempt_uq")
    con.execute("ALTER TABLE llm_calls DROP COLUMN tags")
    con.execute("DROP TABLE llm_usage_events")
    con.execute("DROP TABLE llm_usage_meta")
    con.execute("PRAGMA user_version = 3")
    con.close()


def _v3_row(con: sqlite3.Connection, call_id: str | None, attempt: int | None, **extra: Any) -> None:
    cols = {
        "started_at_ms": 1_790_000_000_000 + (attempt or 0), "duration_ms": 5, "provider": "p",
        "model": "m", "source": "user", "stream": 0, "finish_reason": "stop", "input_tokens": 10,
        "output_tokens": 4, "total_tokens": 14, "reported_tokens": 14, "estimated_tokens": 0,
        "call_id": call_id, "attempt": attempt, "consumer": "app" if call_id else None,
        "cost_usd": 0.001, **extra,
    }
    con.execute(
        f"INSERT INTO llm_calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )


def test_r12_v3_database_migrates_old_rows_read_and_retry_labels_move(tmp_path) -> None:
    from nanobot.llm_usage.query import UsageFilter, records, totals

    path = tmp_path / "u.sqlite3"
    LLMUsageStore(path)._connect().close()  # noqa: SLF001
    _downgrade_to_v3(path)
    con = sqlite3.connect(path, isolation_level=None)
    _v3_row(con, "c1", 1)
    _v3_row(con, "c1", 2, waste_label="retry", waste_set_by="kernel")  # 1.0 labelled the retry
    _v3_row(con, None, None)  # a row from before attribution existed
    con.close()

    store = LLMUsageStore(path)
    docs = {d["record_id"]: d for d in records(store, UsageFilter())}
    assert docs["c1:1"]["waste_label"] == "retry" and docs["c1:1"]["waste_set_by"] == "kernel"
    assert docs["c1:2"]["waste_label"] is None
    legacy = next(d for d in docs.values() if d["call_id"].startswith("legacy-"))
    assert legacy["consumer"] == "unattributed" and legacy["tags"] == {}
    assert totals(store, UsageFilter())[0].wasted_tokens == 14
    assert store._connect().execute("PRAGMA user_version").fetchone()[0] == 4  # noqa: SLF001
    LLMUsageStore(path)._connect()  # noqa: SLF001 - reopening is a no-op, not a second fix-up
    assert len(records(store, UsageFilter())) == 3


def test_r12_an_older_writer_still_works_on_the_migrated_database(tmp_path) -> None:
    """Rollback: the v4 additions are nullable columns and indexes, so the previous build's
    INSERT (which names neither ``tags`` nor ``dedupe_key``) keeps working."""
    path = tmp_path / "u.sqlite3"
    store = LLMUsageStore(path)
    store.record(_record())
    con = sqlite3.connect(path, isolation_level=None)
    con.execute(
        "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, stream, "
        "finish_reason, call_id, attempt, consumer) VALUES (1, 1, 'p', 'm', 'user', 0, 'stop', "
        "'old', 1, 'app')"
    )
    con.execute(
        "INSERT INTO llm_usage_events (ts_ms, kind, call_id, payload) VALUES (1, 'cache_hit', "
        "'old-hit', '{}')"
    )
    con.close()
    assert store.count() == 2


# -- R10b the paths the ledger does not see ---------------------------------------------------------------


def test_r10_unmetered_model_paths_are_inventoried() -> None:
    """U9 ("every token attributable") holds for everything that goes through an
    ``LLMProvider`` (the ledger observer lives in its base class). These modules call a model
    or embedder WITHOUT one: their spend and tokens are invisible to ``kernel.usage`` and to the
    budget. The list is pinned: a new module that talks to a model API fails here until it is
    metered or documented (schemas/ATTRIBUTION.md, "Not covered")."""
    import re

    root = Path(__file__).resolve().parents[2] / "nanobot"
    endpoint = re.compile(
        r"chat/completions|images/generations|audio/transcriptions|/embeddings|AsyncOpenAI\(|"
        r"AsyncAnthropic\(|messages\.create\(|\.chat\.completions\.create|SentenceTransformer\(",
    )
    metered = re.compile(r"^class \w+\((?:LLMProvider|OpenAICompatProvider)\)", re.M)
    found = {
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if endpoint.search(p.read_text()) and not metered.search(p.read_text())
    }
    # The gateway's OpenAI-compatible HTTP API SERVES /v1/chat/completions (inbound route and
    # its CLI banner); each request becomes an AgentLoop turn whose model calls go through a
    # metered LLMProvider, so these modules call no model themselves.
    inbound_routes = {"api/server.py", "cli/commands.py"}
    assert found - inbound_routes == {
        "core/vec_store.py",  # local embedder: tokens computed, no money
        "providers/image_generation.py",  # billed image generation
        "providers/transcription.py",  # billed speech to text
    }


# -- R13 the reducer and the store agree ----------------------------------------------------------------------


async def test_r13_reference_reducer_agrees_with_the_store(make_kernel) -> None:
    from nanobot.llm_usage.query import reduce_records

    est = reply("ok", usage=LLMUsage.estimated(input_tokens=300, output_tokens=60))
    kernel, _ = make_kernel("not json", '{"a": 1}', est, cache=DictCache())
    who = {"consumer": "x", "agent": "a1", "role": "maker", "session": "s1"}
    await kernel.llm.complete_json("j", schema={"type": "object"}, retries=1,
                                   opts=GenerateOptions(attribution=Attribution(**who)))
    await kernel.llm.generate([user("p")], GenerateOptions(attribution=Attribution(**who)))
    await kernel.llm.generate([user("p")], GenerateOptions(attribution=Attribution(**who)))  # hit
    await kernel.llm.generate([user("q")], GenerateOptions(
        attribution=Attribution(consumer="y", agent="a2")))
    docs = kernel.usage.records(limit=1000)
    names = ("requests", "calls", "retries", "tokens_in", "tokens_out", "cache_read_tokens",
             "cache_write_tokens", "cost_usd", "billed_cost_usd", "estimated_cost_usd",
             "unpriced_requests", "estimated_requests", "estimated_tokens", "failed_requests", "wasted_tokens",
             "wasted_cost_usd", "cache_hits", "saved_tokens_in", "saved_tokens_out",
             "saved_cost_usd")
    for group in ((), ("consumer",), ("consumer", "agent"), ("waste_label",)):
        stored = {tuple(sorted(t.group.items())): t for t in kernel.usage.totals(group)}
        rebuilt = {tuple(sorted(t.group.items())): t for t in reduce_records(docs, group)}
        assert stored.keys() == rebuilt.keys(), group
        for key, sv in stored.items():
            for name in names:
                assert getattr(rebuilt[key], name) == pytest.approx(getattr(sv, name)), (
                    group, key, name)
    twice = reduce_records([*docs, *docs])[0]
    assert twice.requests == reduce_records(docs)[0].requests  # duplicate delivery counts once
