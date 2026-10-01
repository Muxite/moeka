"""Group I: usage store under many writers (FR-051, FR-052, SC-005)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import _mi
from conftest import wait_for


def _read(p: Path) -> dict:
    return json.loads(p.read_text())


def _reader(h, d: Path):
    from moeka import Kernel

    return Kernel(_mi.make_env(h.aux / "reader-st", h.aux / "reader-wk", data_dir=d))


def _launch(h, d: Path, nproc: int, calls: int, *, phase2: int = 0):
    go = h.aux / "go"
    outs, procs, p1, closing = [], [], [], []
    for i in range(nproc):
        out = h.aux / f"u{i}.json"
        extra: list[str] = []
        if phase2:
            p1.append(h.aux / f"p1done{i}")
            closing.append(h.aux / f"closing{i}")
            extra = [str(p1[-1]), str(h.aux / "go2"), str(phase2), str(closing[-1])]
        procs.append(h.python("usage_worker", str(out), str(d), str(h.aux / f"st{i}"),
                              str(h.aux / f"wk{i}"), f"c{i}", str(calls), str(go), *extra))
        outs.append(out)
    time.sleep(2.0)
    go.touch()
    return outs, procs, p1, closing


def _check(h, d: Path, nproc: int, per_proc: int, outs, procs):
    for p in procs:
        assert p.wait(timeout=300) == 0, Path(p.err_path).read_text()[-2000:]
    res = [_read(o) for o in outs]
    assert not any(r["errors"] for r in res), [r["errors"][:2] for r in res]
    k = _reader(h, d)
    try:
        total = k.usage.total()
        assert total.requests == nproc * per_proc
        by_consumer = {f"c{i}": k.usage.total(consumer=f"c{i}").requests for i in range(nproc)}
        assert all(v == per_proc for v in by_consumer.values()), by_consumer
        grouped = k.usage.totals(["consumer"])
        assert sum(t.requests for t in grouped) == total.requests
        loss = k.usage.loss()
        assert loss["write_dropped"] == 0 and loss["pending_writes"] == 0
    finally:
        k.close()
    return res


@pytest.mark.fr("FR-051", "FR-052", "SC-005", "FR-053")
@pytest.mark.timeout(400)
def test_sc005_four_processes_fifty_calls(h):
    d = h.aux / "data"
    outs, procs, _, _ = _launch(h, d, 4, 50)
    res = _check(h, d, 4, 50, outs, procs)
    for r in res:
        assert r["loss_before_close"]["write_dropped"] == 0


@pytest.mark.fr("FR-051", "FR-052", "FR-053")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_eight_processes_no_lost_rows(h):
    d = h.aux / "data"
    outs, procs, _, _ = _launch(h, d, 8, 50)
    _check(h, d, 8, 50, outs, procs)


@pytest.mark.fr("FR-051", "FR-053")
@pytest.mark.timeout(400)
def test_writes_blocked_by_lock_are_flushed_at_close(h):
    """The usage DB is write-locked while the workers make their last calls and start
    closing; the lock lifts 1.5 s later, inside the 5 s close-time flush window."""
    d = h.aux / "data"
    n = 4
    outs, procs, p1, closing = _launch(h, d, n, 20, phase2=10)
    assert wait_for(lambda: all(p.exists() for p in p1), 120), "phase 1 did not finish"
    conn = sqlite3.connect(str(d / "llm_usage.sqlite3"), isolation_level=None, timeout=30)
    conn.execute("BEGIN IMMEDIATE")
    try:
        (h.aux / "go2").touch()
        assert wait_for(lambda: all(c.exists() for c in closing), 120)
        time.sleep(1.5)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    _check(h, d, n, 30, outs, procs)


@pytest.mark.fr("FR-052", "FR-053", "FR-054")
def test_two_kernels_one_process_shared_store(h):
    from moeka import Kernel

    d = h.aux / "data"
    k1 = Kernel(_mi.make_env(h.aux / "s1", h.aux / "w1", data_dir=d), consumer="alpha")
    k2 = Kernel(_mi.make_env(h.aux / "s2", h.aux / "w2", data_dir=d), consumer="beta")
    try:
        _mi.attach_fake(k1)
        _mi.attach_fake(k2)
        for _ in range(3):
            k1.llm.complete_sync("a")
        for _ in range(5):
            k2.llm.complete_sync("b")
        for k in (k1, k2):
            assert k.usage.total().requests == 8
            assert k.usage.total(consumer="alpha").requests == 3
            assert k.usage.total(consumer="beta").requests == 5
        assert (d / "llm_usage.sqlite3").exists()
    finally:
        k1.close()
        k2.close()
