"""Spec 005 FR-044..FR-050: SharedCapBudget, one cap across processes."""

from __future__ import annotations

import multiprocessing
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from moeka import Environment, Kernel, ModelSpec, ProviderSpec
from moeka.budget import CapBudget, SharedCapBudget
from moeka.errors import BudgetExceeded
from moeka.testing import FakeProvider, reply
from nanobot.kernel.budget import CallEstimate
from nanobot.kernel.ledger import LedgerEvent
from nanobot.kernel.trace import MemoryTraceSink

_N = [0]


def _estimate(usd: float | None, tokens: int = 100, **tags: Any) -> CallEstimate:
    _N[0] += 1
    return CallEstimate(
        call_id=f"call-{_N[0]}", alias="main", model="m", provider="p", prompt_tokens=tokens // 2,
        max_output_tokens=tokens // 2, rounds=1, worst_case_tokens=tokens, worst_case_usd=usd,
        tags=tags,
    )


def _event(est: CallEstimate, cost: float | None, tin: int = 10, tout: int = 5,
           outcome: str = "ok") -> LedgerEvent:
    return LedgerEvent(
        trace_id=None, slot=None, tier=None, model="m", provider="p", tokens_in=tin,
        tokens_out=tout, tokens_cache_read=None, latency_ms=1.0, cost_usd=cost,
        source="kernel", call_id=est.call_id, outcome=outcome,
    )


FIGURES = ("cap_usd", "cap_tokens", "spent_usd", "spent_tokens", "reserved_usd",
           "reserved_tokens", "remaining_usd", "remaining_tokens")


def _same(a: dict[str, Any], b: dict[str, Any]) -> None:
    assert a["scope"] == b["scope"]
    for key in FIGURES:
        if a[key] is None or b[key] is None:
            assert a[key] == b[key], key
        else:
            assert a[key] == pytest.approx(b[key], abs=1e-12), key


def _script(budget: Any) -> list[Any]:
    """One scripted sequence; returns the observable outcome of each step."""
    out: list[Any] = []

    def admit(est: CallEstimate) -> Any:
        try:
            res = budget.admit(est)
            out.append("ok")
            return res
        except BudgetExceeded as exc:
            out.append((exc.reason_code, exc.scope))
            return None

    e1 = _estimate(0.10, stage="a")
    r1 = admit(e1)
    budget.settle(r1, _event(e1, 0.04))
    budget.release(r1)
    budget.release(r1)  # second release: no-op
    e2 = _estimate(0.20, stage="a")
    r2 = admit(e2)
    budget.settle(r2, _event(e2, None, tin=7, tout=3))  # unpriced tokens: remainder charged
    budget.release(r2)
    e3 = _estimate(0.05, stage="b")
    r3 = admit(e3)
    budget.settle(r3, _event(e3, None, tin=0, tout=0, outcome="timeout"))
    budget.release(r3)
    e4 = _estimate(0.30, stage="a")  # stage a: 0.04 + 0.20 + 0.30 > 0.5
    admit(e4)
    e5 = _estimate(None)  # unpriced under a USD cap
    admit(e5)
    e6 = _estimate(0.02, tokens=10_000)  # token cap
    admit(e6)
    e7 = _estimate(0.01, stage="c")
    r7 = admit(e7)
    budget.settle(r7, _event(e7, 0.005))
    out.append(budget.snapshot())
    out.append(budget.snapshot("stage", "a"))
    out.append(budget.snapshot("stage", "c"))
    budget.release(r7)
    out.append(budget.snapshot())
    out.append(budget.exposure())
    out.append(budget.exposure("stage", "b"))
    return out


def test_parity_with_cap_budget(tmp_path: Path) -> None:
    caps = {"limit_usd": 1.0, "limit_tokens": 5000, "per_tag": {"stage": 0.5}}
    reference = _script(CapBudget(**caps))
    shared = _script(SharedCapBudget(tmp_path, "parity", **caps))
    assert len(reference) == len(shared)
    for ref, got in zip(reference, shared, strict=True):
        if isinstance(ref, dict):
            _same(got, ref)
        elif isinstance(ref, float):
            assert got == pytest.approx(ref, abs=1e-12)
        else:
            assert got == ref
    codes = [r[0] for r in reference if isinstance(r, tuple)]
    assert codes == ["cap_tag", "unpriced_model", "cap_tokens"]


def test_state_is_shared_between_objects(tmp_path: Path) -> None:
    a = SharedCapBudget(tmp_path, "job", limit_usd=0.05)
    b = SharedCapBudget(tmp_path, "job", limit_usd=0.05)
    res = a.admit(_estimate(0.03))
    assert b.reserved_usd == pytest.approx(0.03)
    with pytest.raises(BudgetExceeded) as info:
        b.admit(_estimate(0.03))
    assert info.value.reason_code == "cap_usd"
    a.release(res)
    b.admit(_estimate(0.03))
    other = SharedCapBudget(tmp_path, "other-job", limit_usd=0.05)
    assert other.spent_usd == 0.0 and other.reserved_usd == 0.0
    assert (tmp_path / "llm_usage.sqlite3").exists()


def test_cap_mismatch_and_reset_caps(tmp_path: Path) -> None:
    first = SharedCapBudget(tmp_path, "job", limit_usd=0.5)
    res = first.admit(_estimate(0.2))
    first.settle(res, _event(_estimate(0.2), 0.0))
    for kwargs in ({"limit_usd": 0.6}, {"limit_usd": 0.5, "limit_tokens": 10},
                   {"limit_usd": 0.5, "per_tag": {"x": 1.0}},
                   {"limit_usd": 0.5, "allow_unpriced": True}):
        with pytest.raises(ValueError, match="'job'"):
            SharedCapBudget(tmp_path, "job", **kwargs)
    SharedCapBudget(tmp_path, "job", limit_usd=0.5)  # same caps: fine
    reset = SharedCapBudget(tmp_path, "job", limit_usd=0.25, reset_caps=True)
    snap = reset.snapshot()
    assert snap["cap_usd"] == 0.25 and snap["reserved_usd"] == pytest.approx(0.2)
    with pytest.raises(BudgetExceeded):
        first.admit(_estimate(0.1))  # the stored (new) caps apply to every object


def test_lock_timeout_fails_closed(tmp_path: Path) -> None:
    budget = SharedCapBudget(tmp_path, "job", limit_usd=1.0, lock_timeout_s=0.2)
    res = budget.admit(_estimate(0.1))
    blocker = sqlite3.connect(tmp_path / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(BudgetExceeded) as info:
            budget.admit(_estimate(0.1))
        assert info.value.reason_code == "budget_unavailable"
        assert time.monotonic() - started < 3
        budget.settle(res, _event(_estimate(0.1), 0.05))  # cannot be written now
        budget.release(res)
        assert budget.pending_writes == 2
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert budget.snapshot()["reserved_usd"] == 0.0  # replayed on the next operation
    assert budget.spent_usd == pytest.approx(0.05)
    assert budget.flush() is True


def _env(tmp_path: Path, state: str, sink: Any = None, data: Path | None = None) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / state, work_dir=tmp_path / f"{state}-work", data_dir=data,
        credentials={"oa": "x"}, providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="m", provider="openai", price_in=1.0,
                          price_out=2.0, max_tokens=100)],
        default_model="main", trace=sink,
    )


def test_unavailable_budget_sends_no_provider_call(tmp_path: Path) -> None:
    data = tmp_path / "data"
    budget = SharedCapBudget(data, "job", limit_usd=1.0, lock_timeout_s=0.1)
    with Kernel(_env(tmp_path, "s", data=data), budget=budget) as kernel:
        fake = FakeProvider(default="x")
        kernel.llm.register_provider("main", fake, kernel.env.models["main"])
        blocker = sqlite3.connect(data / "llm_usage.sqlite3", isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(BudgetExceeded) as info:
                kernel.llm.complete_sync("hi")
            assert info.value.reason_code == "budget_unavailable"
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        assert fake.calls == []


def test_kernel_integration_events_and_usage_budget(tmp_path: Path) -> None:
    sink = MemoryTraceSink()
    data = tmp_path / "data"
    budget = SharedCapBudget(data, "job", limit_usd=0.001)
    with Kernel(_env(tmp_path, "s", sink, data), budget=budget) as kernel:
        fake = FakeProvider(default=reply("ok", input_tokens=10, output_tokens=5))
        kernel.llm.register_provider("main", fake, kernel.env.models["main"])
        kernel.llm.complete_sync("hi")
        figures = kernel.usage.budget()
        assert figures is not None and figures["cap_usd"] == 0.001
        assert figures["spent_usd"] == pytest.approx(10e-6 + 10e-6)
        with pytest.raises(BudgetExceeded):
            for _ in range(100):
                kernel.llm.complete_sync("hi")
    names = [e["event"] for e in sink.events]
    assert "budget.admit" in names and "budget.refuse" in names
    admit = next(e for e in sink.events if e["event"] == "budget.admit")
    assert admit["cap_usd"] == 0.001 and admit["reserved_usd"] is not None


def test_lease_expiry_charges_and_late_settle_trues_up(tmp_path: Path) -> None:
    sink = MemoryTraceSink()
    budget = SharedCapBudget(tmp_path / "data", "job", limit_usd=1.0, lease_s=0.2)
    control = CapBudget(limit_usd=1.0)
    with Kernel(_env(tmp_path, "s", sink, tmp_path / "data"), budget=budget):
        est = _estimate(0.3, tokens=200)
        res, cres = budget.admit(est), control.admit(est)
        time.sleep(0.35)
        snap = budget.snapshot()
        assert snap["reserved_usd"] == 0.0 and snap["spent_usd"] == pytest.approx(0.3)
        assert snap["reserved_tokens"] == 0 and snap["spent_tokens"] == 200
        expire = [e for e in sink.events if e["event"] == "budget.expire"]
        assert len(expire) == 1
        assert expire[0]["call_id"] == est.call_id
        assert expire[0]["charged_usd"] == pytest.approx(0.3)
        assert expire[0]["charged_tokens"] == 200
        event = _event(est, 0.1, tin=40, tout=20)
        budget.settle(res, event)
        control.settle(cres, event)
        budget.release(res)
        control.release(cres)
        _same(budget.snapshot(), control.snapshot())


def test_expired_unpriced_reservation_stays_charged(tmp_path: Path) -> None:
    budget = SharedCapBudget(tmp_path, "job", limit_usd=1.0, lease_s=0.1)
    control = CapBudget(limit_usd=1.0)
    est = _estimate(0.2, tokens=100)
    res, cres = budget.admit(est), control.admit(est)
    time.sleep(0.2)
    budget.snapshot()
    event = _event(est, None, outcome="timeout", tin=0, tout=0)
    budget.settle(res, event)
    control.settle(cres, event)
    budget.release(res)
    control.release(cres)
    _same(budget.snapshot(), control.snapshot())
    assert budget.spent_usd == pytest.approx(0.2)


def _worker(data_dir: str, n: int, queue: Any) -> None:
    from moeka.budget import SharedCapBudget as Shared
    from moeka.errors import BudgetExceeded as Refused

    budget = Shared(data_dir, "job", limit_usd=0.50)
    admitted = refused = 0
    codes: set[str] = set()
    worst = 0.0
    for _ in range(n):
        est = _estimate(0.01)
        try:
            res = budget.admit(est)
        except Refused as exc:
            refused += 1
            codes.add(exc.reason_code)
            continue
        admitted += 1
        snap = budget.snapshot()
        worst = max(worst, snap["spent_usd"] + snap["reserved_usd"])
        budget.settle(res, _event(est, 0.01))
        budget.release(res)
    budget.flush()
    queue.put((admitted, refused, sorted(codes), worst))


def test_four_processes_share_one_cap(tmp_path: Path) -> None:
    SharedCapBudget(tmp_path, "job", limit_usd=0.50)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), 50, queue)) for _ in range(4)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=240) for _ in procs]
    for proc in procs:
        proc.join(60)
    admitted = sum(r[0] for r in results)
    refused = sum(r[1] for r in results)
    # FR-045: CapBudget's float arithmetic; 49 x 0.01 accumulates past 0.49 (erratum US3-1).
    assert 49 <= admitted <= 50 and admitted + refused == 200
    assert {code for r in results for code in r[2]} <= {"cap_usd"}
    assert max(r[3] for r in results) <= 0.50 + 1e-9
    final = SharedCapBudget(tmp_path, "job", limit_usd=0.50)
    assert final.spent_usd <= 0.50 + 1e-9 and final.reserved_usd == 0.0


_HOLDER = textwrap.dedent("""
    import sys, time
    from moeka.budget import SharedCapBudget
    from nanobot.kernel.budget import CallEstimate
    budget = SharedCapBudget(sys.argv[1], "job", limit_usd=1.0, lease_s=1.0)
    budget.admit(CallEstimate(call_id="doomed", alias=None, model="m", provider="p",
                              prompt_tokens=1, max_output_tokens=1, rounds=1,
                              worst_case_tokens=50, worst_case_usd=0.4))
    print("reserved", flush=True)
    time.sleep(120)
""")


def test_sigkilled_holder_reservation_is_charged_after_the_lease(tmp_path: Path) -> None:
    budget = SharedCapBudget(tmp_path, "job", limit_usd=1.0, lease_s=1.0)
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "reserved"
        assert budget.reserved_usd == pytest.approx(0.4)
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        time.sleep(1.2)
        snap = budget.snapshot()
        assert snap["reserved_usd"] == 0.0 and snap["spent_usd"] == pytest.approx(0.4)
        with pytest.raises(BudgetExceeded):
            budget.admit(_estimate(0.7))
        budget.admit(_estimate(0.6))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_cap_budget_docstring_says_per_process() -> None:
    assert "per process" in (CapBudget.__doc__ or "")


def test_budget_built_while_the_database_is_locked_fails_closed_at_admission(
    tmp_path: Path,
) -> None:
    SharedCapBudget(tmp_path, "existing", limit_usd=1.0)  # the database exists
    blocker = sqlite3.connect(tmp_path / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        budget = SharedCapBudget(tmp_path, "job", limit_usd=0.5, lock_timeout_s=0.2)
        with pytest.raises(ValueError):
            SharedCapBudget(tmp_path, "existing", limit_usd=2.0, lock_timeout_s=0.2)
        started = time.monotonic()
        with pytest.raises(BudgetExceeded) as info:
            budget.admit(_estimate(0.1))
        assert info.value.reason_code == "budget_unavailable"
        assert time.monotonic() - started < 2
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    res = budget.admit(_estimate(0.1))  # caps are written with the first admission
    assert budget.snapshot()["cap_usd"] == 0.5
    with pytest.raises(ValueError):
        SharedCapBudget(tmp_path, "job", limit_usd=0.6)
    budget.release(res)


def test_pending_settle_is_written_at_kernel_close(tmp_path: Path) -> None:
    data = tmp_path / "data"
    budget = SharedCapBudget(data, "job", limit_usd=1.0, lock_timeout_s=0.3)
    kernel = Kernel(_env(tmp_path, "s", data=data), budget=budget)
    res = budget.admit(_estimate(0.2))
    blocker = sqlite3.connect(data / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        budget.settle(res, _event(_estimate(0.2), 0.05))
        budget.release(res)
        assert budget.pending_writes == 2
        assert budget.reserved_usd == pytest.approx(0.2)  # still counted while unwritten
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    kernel.close()
    assert budget.pending_writes == 0
    other = SharedCapBudget(data, "job", limit_usd=1.0)
    assert other.spent_usd == pytest.approx(0.05) and other.reserved_usd == 0.0


def test_replay_does_not_delay_a_refusal(tmp_path: Path) -> None:
    budget = SharedCapBudget(tmp_path, "job", limit_usd=1.0, lock_timeout_s=0.3)
    res = budget.admit(_estimate(0.1))
    blocker = sqlite3.connect(tmp_path / "llm_usage.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        for _ in range(5):
            budget.settle(res, _event(_estimate(0.1), 0.01))
        started = time.monotonic()
        with pytest.raises(BudgetExceeded):
            budget.admit(_estimate(0.1))
        assert time.monotonic() - started < 0.3 + 1.0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
