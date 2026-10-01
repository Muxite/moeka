"""Spec 005 FR-051/FR-052 (SC-005): many writers on one usage store lose no row."""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path
from typing import Any

from nanobot.llm_usage.store import LLMUsageStore


def _worker(tmp: str, index: int, calls: int, queue: Any) -> None:
    from moeka import Environment, Kernel, ModelSpec, ProviderSpec
    from moeka.testing import FakeProvider, reply

    root = Path(tmp)
    env = Environment.for_host(
        state_dir=root / f"state-{index}", work_dir=root / f"work-{index}",
        data_dir=root / "data", credentials={"oa": "x"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="m", provider="openai", price_in=1.0,
                          price_out=1.0)],
        default_model="main",
    )
    kernel = Kernel(env, consumer=f"worker-{index}")
    kernel.llm.register_provider(
        "main", FakeProvider(default=reply("ok", input_tokens=3, output_tokens=2)),
        env.models["main"],
    )
    for _ in range(calls):
        kernel.llm.complete_sync("hi")
    kernel.close()
    queue.put(index)


def test_four_processes_fifty_calls_each(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), i, 50, queue)) for i in range(4)]
    for proc in procs:
        proc.start()
    done = sorted(queue.get(timeout=300) for _ in procs)
    for proc in procs:
        proc.join(60)
    assert done == [0, 1, 2, 3]

    from moeka import Environment, Kernel, ModelSpec, ProviderSpec

    env = Environment.for_host(
        state_dir=tmp_path / "reader", work_dir=tmp_path / "reader-work",
        data_dir=tmp_path / "data", credentials={},
        providers=[ProviderSpec(name="openai")],
        models=[ModelSpec(name="main", model="m", provider="openai")], default_model="main",
    )
    with Kernel(env) as kernel:
        total = kernel.usage.total()
        assert total.requests == 200
        per = [kernel.usage.total(consumer=f"worker-{i}").requests for i in range(4)]
        assert per == [50, 50, 50, 50]
        grouped = kernel.usage.totals(["consumer"])
        assert sum(g.requests for g in grouped) == total.requests
        loss = kernel.usage.loss()
        assert loss["write_dropped"] == 0 and loss["pending_writes"] == 0


def test_close_flushes_pending_writes(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm_usage.sqlite3")
    store._connect()
    blocker = sqlite3.connect(tmp_path / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    event = {"event": "call.waste", "call_id": "c1", "attempt": 1, "waste_label": "retry"}
    try:
        try:
            store.record_usage_event(event)
        except sqlite3.Error:
            pass
        assert store.stats()["pending_writes"] == 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    store.close()  # the lock is free now: close lands the pending write
    assert store.stats()["pending_writes"] == 0
    rows = sqlite3.connect(tmp_path / "llm_usage.sqlite3").execute(
        "SELECT COUNT(*) FROM llm_usage_events"
    ).fetchone()[0]
    assert rows == 1
    store.close()


def test_flush_gives_up_after_its_deadline(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm_usage.sqlite3")
    store._connect()
    blocker = sqlite3.connect(tmp_path / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        try:
            store.record_usage_event({"event": "call.waste", "call_id": "c2", "attempt": 1,
                                      "waste_label": "retry"})
        except sqlite3.Error:
            pass
        assert store.flush(timeout_s=0.3) is False
        assert store.stats()["pending_writes"] == 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert store.flush() is True
    store.close()
