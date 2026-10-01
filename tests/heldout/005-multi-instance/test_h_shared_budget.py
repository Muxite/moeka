"""Group H: cross-process SharedCapBudget (FR-044..FR-050, US3, SC-004, SC-006)."""

from __future__ import annotations

import json
import math
import os
import signal
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import _mi
from conftest import wait_for

EPS = 1e-9


def _read(p: Path) -> dict:
    return json.loads(p.read_text())


def _go_file(h) -> Path:
    return h.aux / "go"


def _snap_close(a: dict, b: dict) -> None:
    assert set(a) == set(b)
    for k in a:
        va, vb = a[k], b[k]
        if isinstance(va, float) or isinstance(vb, float):
            assert va is not None and vb is not None and math.isclose(va, vb, abs_tol=1e-9), k
        else:
            assert va == vb, (k, va, vb)


# -- FR-044 surface ---------------------------------------------------------------------------------


@pytest.mark.fr("FR-044")
def test_surface_and_storage(h):
    from moeka.budget import Budget, CapBudget, SharedCapBudget

    d = h.aux / "data"
    b = SharedCapBudget(d, "job", limit_usd=1.0)
    assert isinstance(b, Budget)
    assert (d / "llm_usage.sqlite3").exists()
    assert set(b.snapshot()) == set(CapBudget(limit_usd=1.0).snapshot())
    assert set(b.snapshot("stage", "x")) == set(CapBudget(limit_usd=1.0).snapshot("stage", "x"))
    for prop in ("spent_usd", "spent_tokens", "reserved_usd", "reserved_tokens"):
        assert getattr(b, prop) == 0
        with pytest.raises(AttributeError):
            setattr(b, prop, 1)
    assert b.exposure() == 0.0


@pytest.mark.fr("FR-044")
def test_state_shared_by_data_dir_and_id(h):
    d = h.aux / "data"
    b1 = _mi.shared_budget(d, "job", limit_usd=1.0)
    link = h.aux / "data-link"
    link.symlink_to(d)
    b2 = _mi.shared_budget(link, "job", limit_usd=1.0)
    other = _mi.shared_budget(d, "other-job", limit_usd=1.0)
    r = b1.admit(_mi.estimate("c1", 0.3, tokens=40))
    assert math.isclose(b2.reserved_usd, 0.3) and b2.reserved_tokens == 40
    assert math.isclose(b2.exposure(), 0.3)
    assert other.reserved_usd == 0
    b1.settle(r, _mi.event(0.1))
    b1.release(r)
    assert math.isclose(b2.spent_usd, 0.1) and b2.reserved_usd == 0


@pytest.mark.fr("FR-044")
def test_state_shared_across_processes(h):
    d = h.aux / "data"
    out = h.aux / "hang.json"
    p = h.python("budget_hang", str(out), str(d), "job", "1.0", "0.25", "77", "3600")
    assert wait_for(out.exists, 60)
    b = _mi.shared_budget(d, "job", limit_usd=1.0)
    assert math.isclose(b.reserved_usd, 0.25) and b.reserved_tokens == 77
    p.kill()


# -- FR-045 parity with CapBudget --------------------------------------------------------------------


def _script():
    """(op, args) steps; admits name their reservation."""
    return [
        ("admit", "r1", 0.20, 100, {"stage": "a"}),
        ("admit", "r2", 0.15, 50, {"stage": "b"}),
        ("admit", "r3", 0.25, 80, {"stage": "a"}),   # stage a hits 0.45
        ("admit", "r4", 0.10, 10, {"stage": "a"}),   # stage a cap 0.5 -> refused cap_tag
        ("settle", "r1", 0.05, None),
        ("release", "r1"),
        ("admit", "r5", None, 10, {}),               # unpriced under a USD cap
        ("admit", "r6", 0.01, 5000, {}),              # token cap
        ("settle", "r2", None, "tokens"),             # unpriced usage -> charge at release
        ("release", "r2"),
        ("release", "r2"),                            # second release is a no-op
        ("settle", "r3", None, "timeout"),            # timeout -> charge remainder
        ("release", "r3"),
        ("admit", "r7", 0.50, 10, {}),               # total cap 1.0
        ("admit", "r8", 0.30, 10, {"stage": "c"}),
    ]


def _run_script(budget):
    log = []
    held = {}
    for step in _script():
        op = step[0]
        if op == "admit":
            _, name, usd, tokens, tags = step
            try:
                held[name] = budget.admit(_mi.estimate(name, usd, tokens=tokens, tags=tags))
                log.append(("ok", name))
            except Exception as exc:  # noqa: BLE001
                log.append(("refused", name, getattr(exc, "reason_code", None),
                            getattr(exc, "scope", None), type(exc).__name__))
        elif op == "settle":
            _, name, cost, kind = step
            if name not in held:
                continue
            if kind == "tokens":
                budget.settle(held[name], _mi.event(None, tokens_in=7, tokens_out=3))
            elif kind == "timeout":
                budget.settle(held[name], _mi.event(None, tokens_in=0, tokens_out=0,
                                                    outcome="timeout"))
            else:
                budget.settle(held[name], _mi.event(cost))
        else:
            _, name = step
            if name in held:
                budget.release(held[name])
        snaps = [budget.snapshot()] + [budget.snapshot("stage", v) for v in ("a", "b", "c")]
        log.append(("snap", snaps))
    return log


@pytest.mark.fr("FR-045", "FR-046")
def test_parity_with_capbudget(h):
    from moeka.budget import CapBudget

    caps = dict(limit_usd=1.0, limit_tokens=2000, per_tag={"stage": 0.5})
    ref = _run_script(CapBudget(**caps))
    got = _run_script(_mi.shared_budget(h.aux / "data", "parity", **caps))
    assert len(ref) == len(got)
    for a, b in zip(ref, got):
        assert a[0] == b[0], (a, b)
        if a[0] == "snap":
            for sa, sb in zip(a[1], b[1]):
                _snap_close(sa, sb)
        else:
            assert a == b
    codes = {x[2] for x in ref if x[0] == "refused"}
    assert {"cap_tag", "unpriced_model", "cap_tokens"} <= codes


@pytest.mark.fr("FR-045")
def test_refusal_is_budget_exceeded_cap_usd(h):
    from moeka.errors import BudgetExceeded

    b = _mi.shared_budget(h.aux / "data", "job", limit_usd=0.1)
    b.admit(_mi.estimate("a", 0.08))
    with pytest.raises(BudgetExceeded) as info:
        b.admit(_mi.estimate("b", 0.05))
    assert info.value.reason_code == "cap_usd"


@pytest.mark.fr("FR-045", "FR-046")
def test_unpriced_allowed_and_charged(h):
    b = _mi.shared_budget(h.aux / "data", "job", limit_usd=1.0, allow_unpriced=True)
    r = b.admit(_mi.estimate("u", None))
    b.settle(r, _mi.event(None))
    b.release(r)
    assert b.reserved_usd == 0


# -- US3-1 acceptance + SC-004 invariant (multi-process) -------------------------------------------


def _run_fixed(h, nproc: int, n: int, usd: float, limit: float):
    d = h.aux / "data"
    _mi.shared_budget(d, "job", limit_usd=limit)  # create tables + caps first
    go = _go_file(h)
    outs = [h.aux / f"fixed{i}.json" for i in range(nproc)]
    procs = [h.python("budget_fixed", str(o), str(d), "job", str(limit), str(n), str(usd),
                      str(go)) for o in outs]
    time.sleep(1.0)
    go.touch()
    for p in procs:
        assert p.wait(timeout=240) == 0, Path(p.err_path).read_text()[-2000:]
    res = [_read(o) for o in outs]
    return d, res


@pytest.mark.fr("FR-045", "FR-044")
@pytest.mark.timeout(300)
def test_us3_exactly_cap_admitted_exact_amounts(h):
    usd = 2.0 ** -7  # 0.0078125: exact in binary, so the cap holds exactly 50 calls
    d, res = _run_fixed(h, 4, 50, usd, 50 * usd)
    admitted = sum(r["admitted"] for r in res)
    refused = sum(sum(r["refused"].values()) for r in res)
    assert admitted == 50 and refused == 150
    assert all(set(r["refused"]) <= {"cap_usd"} for r in res)
    assert not any(r["errors"] for r in res)
    b = _mi.shared_budget(d, "job", limit_usd=50 * usd)
    assert math.isclose(b.spent_usd, 50 * usd, abs_tol=EPS) and b.reserved_usd == 0


@pytest.mark.fr("FR-045", "FR-044")
@pytest.mark.timeout(300)
def test_us3_literal_scenario(h):
    """Spec US3-1 with $0.01 against $0.50: float addition of 0.01 lets CapBudget's own
    arithmetic admit 49 or 50; the cross-process total must match that and never more."""
    d, res = _run_fixed(h, 4, 50, 0.01, 0.50)
    admitted = sum(r["admitted"] for r in res)
    refused = sum(sum(r["refused"].values()) for r in res)
    assert 49 <= admitted <= 50 and admitted + refused == 200
    assert all(set(r["refused"]) <= {"cap_usd"} for r in res)
    b = _mi.shared_budget(d, "job", limit_usd=0.50)
    assert b.spent_usd <= 0.50 + EPS


def _stress_run(h, run: int, nproc: int, limit: float):
    d = h.aux / f"data{run}"
    _mi.shared_budget(d, "stress", limit_usd=limit)
    go = h.aux / f"go{run}"
    stop = h.aux / f"stop{run}"
    mon_out, mon_ready = h.aux / f"mon{run}.json", h.aux / f"monr{run}.json"
    mon = h.python("budget_monitor", str(mon_out), str(d), "stress", str(limit), str(stop),
                   str(mon_ready))
    outs = [h.aux / f"st{run}-{i}.json" for i in range(nproc)]
    procs = [h.python("budget_stress", str(o), str(d), "stress", str(limit), "50",
                      str(run * 1000 + i), str(go)) for i, o in enumerate(outs)]
    assert wait_for(mon_ready.exists, 60)
    time.sleep(1.0)
    go.touch()
    for p in procs:
        assert p.wait(timeout=300) == 0, Path(p.err_path).read_text()[-2000:]
    stop.touch()
    assert mon.wait(timeout=60) == 0
    res = [_read(o) for o in outs]
    m = _read(mon_out)
    final = _mi.shared_budget(d, "stress", limit_usd=limit)
    return res, m, final


@pytest.mark.fr("FR-045", "SC-004")
@pytest.mark.slow
@pytest.mark.timeout(1500)
def test_sc004_cap_never_overshoots_under_contention(h):
    runs = int(os.environ.get("HELDOUT_SC004_RUNS", "10"))
    nproc = int(os.environ.get("HELDOUT_SC004_PROCS", "8"))
    limit = 0.5
    for run in range(runs):
        res, mon, final = _stress_run(h, run, nproc, limit)
        assert mon["samples"] > 0
        assert mon["max_exposure"] <= limit + EPS, f"run {run}: monitor saw overshoot"
        for r in res:
            assert r["max_exposure"] <= limit + EPS, f"run {run}: worker saw overshoot"
            assert not r["errors"], r["errors"][:3]
            assert set(r["refused"]) <= {"cap_usd"}, r["refused"]
        assert sum(r["admitted"] for r in res) > 0
        assert sum(sum(r["refused"].values()) for r in res) > 0, "cap never reached"
        assert final.spent_usd <= limit + EPS
        assert final.reserved_usd == pytest.approx(0, abs=EPS)


# -- FR-046 / FR-047 lease expiry ------------------------------------------------------------------


@pytest.mark.fr("FR-047", "SC-006")
@pytest.mark.timeout(120)
def test_sigkilled_reservation_is_charged_after_lease(h):
    d = h.aux / "data"
    lease = 1.5
    _mi.shared_budget(d, "job", limit_usd=1.0, lease_s=lease)
    out = h.aux / "hang.json"
    p = h.python("budget_hang", str(out), str(d), "job", "1.0", "0.3", "120", str(lease))
    assert wait_for(out.exists, 60)
    t_admit = _read(out)["t"]
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    b = _mi.shared_budget(d, "job", limit_usd=1.0, lease_s=lease)
    assert math.isclose(b.snapshot()["reserved_usd"], 0.3, abs_tol=EPS)
    reconciled = None
    while time.time() - t_admit < lease + 1:
        snap = b.snapshot()
        if snap["reserved_usd"] == pytest.approx(0, abs=EPS):
            reconciled = snap
            break
        time.sleep(0.05)
    assert reconciled is not None, "expired reservation not reconciled within lease_s + 1"
    assert math.isclose(reconciled["spent_usd"], 0.3, abs_tol=EPS)
    assert reconciled["spent_tokens"] == 120 and reconciled["reserved_tokens"] == 0
    # Charged, not reserved: the cap now has 0.7 left.
    b.admit(_mi.estimate("next", 0.7))
    with pytest.raises(Exception):
        b.admit(_mi.estimate("over", 0.01))


@pytest.mark.fr("FR-047")
@pytest.mark.timeout(120)
def test_budget_expire_trace_event(h):
    from moeka import Kernel

    d = h.aux / "data"
    lease = 1.0
    out = h.aux / "hang.json"
    _mi.shared_budget(d, "job", limit_usd=1.0, lease_s=lease)
    p = h.python("budget_hang", str(out), str(d), "job", "1.0", "0.2", "60", str(lease))
    assert wait_for(out.exists, 60)
    call_id = _read(out)["call_id"]
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    time.sleep(lease + 0.3)
    sink = _mi.Sink()
    budget = _mi.shared_budget(d, "job", limit_usd=1.0, lease_s=lease)
    k = Kernel(_mi.make_env(h.aux / "st", h.aux / "wk", data_dir=d, trace=sink), budget=budget)
    try:
        _mi.attach_fake(k)
        k.llm.complete_sync("trigger an admission")
        ev = [e for e in sink.named("budget.expire") if e.get("call_id") == call_id]
        assert len(ev) == 1, sink.named("budget.expire")
        usd = [v for key, v in ev[0].items() if "usd" in key and isinstance(v, (int, float))]
        tok = [v for key, v in ev[0].items() if "token" in key and isinstance(v, int)]
        assert any(math.isclose(v, 0.2, abs_tol=EPS) for v in usd), ev[0]
        assert 60 in tok, ev[0]
    finally:
        k.close()


@pytest.mark.fr("FR-046", "FR-047")
@pytest.mark.timeout(60)
def test_late_settle_and_release_true_up(h):
    b = _mi.shared_budget(h.aux / "data", "job", limit_usd=1.0, lease_s=0.5)
    r = b.admit(_mi.estimate("slow", 0.5, tokens=100))
    time.sleep(0.8)
    snap = b.snapshot()
    assert math.isclose(snap["spent_usd"], 0.5, abs_tol=EPS) and snap["reserved_usd"] == 0
    b.settle(r, _mi.event(0.2, tokens_in=30, tokens_out=10))
    b.release(r)
    snap = b.snapshot()
    assert math.isclose(snap["spent_usd"], 0.2, abs_tol=EPS), snap
    assert snap["reserved_usd"] == pytest.approx(0, abs=EPS)
    assert snap["spent_tokens"] == 40


@pytest.mark.fr("FR-046", "FR-047")
@pytest.mark.timeout(60)
def test_late_unpriced_settle_keeps_charge(h):
    b = _mi.shared_budget(h.aux / "data", "job", limit_usd=1.0, lease_s=0.5)
    r = b.admit(_mi.estimate("slow", 0.5, tokens=100))
    time.sleep(0.8)
    b.snapshot()
    b.settle(r, _mi.event(None, tokens_in=10, tokens_out=10))
    b.release(r)
    snap = b.snapshot()
    assert math.isclose(snap["spent_usd"], 0.5, abs_tol=EPS), snap
    assert snap["reserved_usd"] == pytest.approx(0, abs=EPS)


@pytest.mark.fr("FR-046")
def test_release_twice_noop_and_timeout_charged(h):
    from moeka.budget import CapBudget

    for b in (CapBudget(limit_usd=1.0), _mi.shared_budget(h.aux / "data", "j", limit_usd=1.0)):
        r = b.admit(_mi.estimate("t", 0.4))
        b.settle(r, _mi.event(None, tokens_in=0, tokens_out=0, outcome="cancelled"))
        b.release(r)
        b.release(r)
        assert math.isclose(b.spent_usd, 0.4, abs_tol=EPS) and b.reserved_usd == 0


# -- FR-048 lock timeout -----------------------------------------------------------------------------


class _DbWriteLock:
    """Holds the SQLite write lock on the usage store (no table is read)."""

    def __init__(self, db: Path):
        # check_same_thread=False: release_later() releases from a helper thread.
        self.conn = sqlite3.connect(str(db), isolation_level=None, timeout=30,
                                    check_same_thread=False)
        self.conn.execute("BEGIN IMMEDIATE")
        self.released = False
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def release(self):
        try:
            self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
        self.released = True

    def release_later(self, delay: float) -> None:
        def run():
            time.sleep(delay)
            try:
                self.release()
            except BaseException as exc:  # noqa: BLE001 - surfaced by join()
                self._error = exc

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def join(self, timeout: float = 10) -> None:
        """Wait for release_later(); fail loudly if the release did not happen."""
        if self._thread is not None:
            self._thread.join(timeout)
        if self._error is not None:
            raise AssertionError(f"releasing the test's DB write lock failed: {self._error!r}")
        assert self.released, "the test's DB write lock was never released"


@pytest.mark.fr("FR-048")
@pytest.mark.timeout(60)
def test_admission_fails_closed_when_db_locked(h):
    from moeka.errors import BudgetExceeded

    d = h.aux / "data"
    b = _mi.shared_budget(d, "job", limit_usd=1.0, lock_timeout_s=0.5)
    lock = _DbWriteLock(d / "llm_usage.sqlite3")
    try:
        t0 = time.monotonic()
        with pytest.raises(BudgetExceeded) as info:
            b.admit(_mi.estimate("x", 0.1))
        assert info.value.reason_code == "budget_unavailable"
        assert time.monotonic() - t0 < 5
    finally:
        lock.release()
    b.admit(_mi.estimate("y", 0.1))


@pytest.mark.fr("FR-048", "FR-050")
@pytest.mark.timeout(60)
def test_no_provider_call_when_budget_unavailable(h):
    from moeka import Kernel
    from moeka.errors import BudgetExceeded

    d = h.aux / "data"
    budget = _mi.shared_budget(d, "job", limit_usd=1.0, lock_timeout_s=0.5)
    k = Kernel(_mi.make_env(h.aux / "st", h.aux / "wk", data_dir=d), budget=budget)
    try:
        fake = _mi.attach_fake(k)
        lock = _DbWriteLock(d / "llm_usage.sqlite3")
        try:
            with pytest.raises(BudgetExceeded) as info:
                k.llm.complete_sync("hi")
            assert info.value.reason_code == "budget_unavailable"
            assert fake.calls == []
        finally:
            lock.release()
    finally:
        k.close()


@pytest.mark.fr("FR-048")
@pytest.mark.timeout(60)
def test_unwritten_settle_is_retried_and_stays_counted(h):
    d = h.aux / "data"
    b = _mi.shared_budget(d, "job", limit_usd=1.0, lock_timeout_s=0.3)
    other = _mi.shared_budget(d, "job", limit_usd=1.0, lock_timeout_s=0.3)
    r = b.admit(_mi.estimate("x", 0.5))
    lock = _DbWriteLock(d / "llm_usage.sqlite3")
    try:
        for fn in (lambda: b.settle(r, _mi.event(0.2)), lambda: b.release(r)):
            try:
                fn()
            except Exception:  # noqa: BLE001 - only the eventual state is specified
                pass
        snap = other.snapshot()
        assert math.isclose(snap["spent_usd"] + snap["reserved_usd"], 0.5, abs_tol=EPS), snap
    finally:
        lock.release()
    b.snapshot()  # next operation of that object replays the queued writes
    snap = other.snapshot()
    assert math.isclose(snap["spent_usd"], 0.2, abs_tol=EPS), snap
    assert snap["reserved_usd"] == pytest.approx(0, abs=EPS)


@pytest.mark.fr("FR-048")
@pytest.mark.timeout(60)
def test_unwritten_settle_flushed_at_kernel_close(h):
    from moeka import Kernel

    d = h.aux / "data"
    b = _mi.shared_budget(d, "job", limit_usd=1.0, lock_timeout_s=0.3)
    k = Kernel(_mi.make_env(h.aux / "st", h.aux / "wk", data_dir=d), budget=b)
    r = b.admit(_mi.estimate("x", 0.5))
    lock = _DbWriteLock(d / "llm_usage.sqlite3")
    try:
        for fn in (lambda: b.settle(r, _mi.event(0.2)), lambda: b.release(r)):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
    finally:
        # Lift the lock before close() and touch `b` no further, so the close-time flush
        # is the first chance to write the queued settle/release (no timing race).
        lock.release()
    assert lock.released
    k.close()
    snap = _mi.shared_budget(d, "job", limit_usd=1.0).snapshot()
    assert math.isclose(snap["spent_usd"], 0.2, abs_tol=EPS), snap
    assert snap["reserved_usd"] == pytest.approx(0, abs=EPS)


# -- FR-049 caps ----------------------------------------------------------------------------------


@pytest.mark.fr("FR-049")
def test_cap_mismatch_and_reset(h):
    d = h.aux / "data"
    b1 = _mi.shared_budget(d, "job-x", limit_usd=1.0)
    r = b1.admit(_mi.estimate("a", 0.4))
    _mi.shared_budget(d, "job-x", limit_usd=1.0)  # same caps: fine
    for kw in (dict(limit_usd=2.0), dict(limit_usd=1.0, limit_tokens=10),
               dict(limit_usd=1.0, per_tag={"s": 0.1}), dict(limit_usd=1.0, allow_unpriced=True)):
        with pytest.raises(ValueError) as info:
            _mi.shared_budget(d, "job-x", **kw)
        assert "job-x" in str(info.value)
    b3 = _mi.shared_budget(d, "job-x", limit_usd=2.0, reset_caps=True)
    snap = b3.snapshot()
    assert snap["cap_usd"] == 2.0
    assert math.isclose(snap["reserved_usd"], 0.4, abs_tol=EPS)
    b1.settle(r, _mi.event(0.1))
    b1.release(r)
    assert math.isclose(b3.spent_usd, 0.1, abs_tol=EPS)


@pytest.mark.fr("FR-049")
def test_cap_mismatch_cross_process(h):
    d = h.aux / "data"
    _mi.shared_budget(d, "job", limit_usd=1.0)
    out = h.aux / "c.json"
    h.worker("budget_construct", str(out), str(d), "job", "3.0")
    res = _read(out)
    assert res["ok"] is False and "ValueError" in res["error"]["mro"]
    assert "job" in res["error"]["message"]


# -- FR-050 kernel integration ----------------------------------------------------------------------


@pytest.mark.fr("FR-050")
def test_kernel_with_shared_budget(h):
    from moeka import Kernel
    from moeka.errors import BudgetExceeded

    d = h.aux / "data"
    sink = _mi.Sink()
    budget = _mi.shared_budget(d, "job", limit_usd=1.0)
    k = Kernel(_mi.make_env(h.aux / "st", h.aux / "wk", data_dir=d, trace=sink), budget=budget)
    try:
        _mi.attach_fake(k)
        k.llm.complete_sync("hi")
        admits = sink.named("budget.admit")
        assert len(admits) == 1
        view = k.usage.budget()
        assert view is not None and view["cap_usd"] == 1.0
        assert view["spent_usd"] > 0 and math.isclose(view["spent_usd"], budget.spent_usd)
        assert "schema_version" in view
    finally:
        k.close()
    tight = _mi.shared_budget(h.aux / "data2", "tight", limit_usd=1e-9)
    sink2 = _mi.Sink()
    k2 = Kernel(_mi.make_env(h.aux / "st2", h.aux / "wk2", data_dir=h.aux / "data2",
                             trace=sink2), budget=tight)
    try:
        fake = _mi.attach_fake(k2)
        with pytest.raises(BudgetExceeded):
            k2.llm.complete_sync("hi")
        assert fake.calls == []
        assert len(sink2.named("budget.refuse")) == 1
    finally:
        k2.close()


@pytest.mark.fr("FR-050", "FR-045")
@pytest.mark.timeout(300)
def test_kernels_in_processes_share_one_cap(h):
    d = h.aux / "data"
    # A fake call settles at (20 in, 5 out) = $3e-5; worst case is about $2.1e-4.
    limit = 0.001
    _mi.shared_budget(d, "jobk", limit_usd=limit)
    go = _go_file(h)
    outs = [h.aux / f"k{i}.json" for i in range(4)]
    procs = [h.python("kernel_budget_calls", str(o), str(h.aux / f"st{i}"), str(h.aux / f"wk{i}"),
                      str(d), "jobk", str(limit), "20", str(go)) for i, o in enumerate(outs)]
    time.sleep(2.0)
    go.touch()
    for p in procs:
        assert p.wait(timeout=240) == 0, Path(p.err_path).read_text()[-2000:]
    res = [_read(o) for o in outs]
    assert not any(r["errors"] for r in res), [r["errors"][:2] for r in res]
    assert sum(r["ok"] for r in res) < 80, "the shared cap never refused"
    assert all(set(r["refused"]) <= {"cap_usd"} for r in res)
    final = _mi.shared_budget(d, "jobk", limit_usd=limit)
    assert final.spent_usd <= limit + EPS
