"""Group F: per-state-dir instance lock and read-only attach (FR-033..FR-039, SC-003, SC-006)."""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import _mi
from conftest import flock_held, free_port, pid_alive, tree, wait_for


def dirs(h, name="s"):
    state = h.aux / f"{name}-state"
    work = h.aux / f"{name}-work"
    return state, work


def kernel(state, work, **kw):
    from moeka import Kernel

    return Kernel(_mi.make_env(state, work), **kw)


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text())


# -- FR-033 holder record ----------------------------------------------------------------------


@pytest.mark.fr("FR-033")
def test_writer_holds_flock_and_writes_holder(h):
    state, work = dirs(h)
    k = kernel(state, work)
    try:
        lock = state / ".instance.lock"
        assert lock.exists()
        out = h.aux / "probe.json"
        r = h.run([sys.executable, "-c", (
            "import fcntl,os,sys,json;fd=os.open(sys.argv[1],os.O_RDONLY)\n"
            "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);r=False\n"
            "except BlockingIOError:\n r=True\n"
            "json.dump({'held':r},open(sys.argv[2],'w'))"), str(lock), str(out)])
        assert r.returncode == 0 and _read_json(out)["held"] is True
        rec = _read_json(state / ".instance.json")
        assert rec["pid"] == os.getpid()
        assert rec["hostname"] == socket.gethostname()
        ts = datetime.fromisoformat(rec["started_at"].replace("Z", "+00:00"))
        assert ts.utcoffset() is not None and ts.utcoffset().total_seconds() == 0
        assert abs((datetime.now(UTC) - ts).total_seconds()) < 120
        assert isinstance(rec["argv0"], str)
        assert rec["mode"] == "write"
    finally:
        k.close()
    assert not flock_held(state / ".instance.lock")


# -- FR-034 refusal --------------------------------------------------------------------------------


def _assert_refused(exc, state: Path, pid: int):
    from moeka.errors import InstanceLockedError

    assert isinstance(exc, InstanceLockedError)
    assert str(state.resolve()) in str(exc)
    assert f"pid {pid}" in str(exc)
    assert Path(exc.state_dir) == state.resolve()
    assert isinstance(exc.holder, dict) and exc.holder.get("pid") == pid
    assert exc.reason == "held"


@pytest.mark.fr("FR-034")
def test_second_writer_in_process_refused(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    k1 = kernel(state, work)
    try:
        t0 = time.monotonic()
        with pytest.raises(InstanceLockedError) as info:
            kernel(state, h.aux / "w2")
        assert time.monotonic() - t0 < 2
        _assert_refused(info.value, state, os.getpid())
    finally:
        k1.close()


@pytest.mark.fr("FR-034")
def test_second_writer_via_symlink_refused(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    k1 = kernel(state, work)
    link = h.aux / "state-link"
    link.symlink_to(state)
    try:
        with pytest.raises(InstanceLockedError) as info:
            kernel(link, h.aux / "w2")
        assert Path(info.value.state_dir) == state.resolve()
    finally:
        k1.close()


@pytest.mark.fr("FR-034", "SC-003")
@pytest.mark.timeout(300)
def test_sc003_twenty_refusals_write_nothing(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    k1 = kernel(state, work)
    try:
        time.sleep(0.2)
        sessions_sibling = state.parent / f"{state.name}-sessions"
        before = tree(state, sessions_sibling)
        for i in range(10):
            t0 = time.monotonic()
            with pytest.raises(InstanceLockedError):
                kernel(state, h.aux / f"w-in{i}")
            assert time.monotonic() - t0 < 2
            assert tree(state, sessions_sibling) == before, f"in-process attempt {i} wrote"
        for i in range(10):
            out = h.aux / f"try{i}.json"
            r = h.worker("kernel_try", str(out), str(state), str(h.aux / f"w-x{i}"))
            assert r.returncode == 0, r.stderr[-2000:]
            res = _read_json(out)
            assert res["ok"] is False and res["error"]["type"] == "InstanceLockedError"
            assert res["elapsed"] < 2
            assert f"pid {os.getpid()}" in res["error"]["message"]
            assert str(state.resolve()) in res["error"]["message"]
            assert res["error"]["holder"]["pid"] == os.getpid()
            assert res["error"]["reason"] == "held"
            assert tree(state, sessions_sibling) == before, f"cross-process attempt {i} wrote"
    finally:
        k1.close()


@pytest.mark.fr("FR-034", "FR-033")
def test_child_writer_blocks_parent(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    ready = h.aux / "ready.json"
    p = h.python("kernel_hold", str(state), str(work), str(ready))
    assert wait_for(ready.exists, 60), "child kernel did not start"
    child = _read_json(ready)["pid"]
    with pytest.raises(InstanceLockedError) as info:
        kernel(state, h.aux / "w2")
    _assert_refused(info.value, state, child)
    p.kill()


# -- FR-035 release ----------------------------------------------------------------------------


@pytest.mark.fr("FR-035")
def test_close_releases_lock(h):
    state, work = dirs(h)
    k1 = kernel(state, work)
    k1.close()
    k2 = kernel(state, work)
    k2.close()
    with kernel(state, work):
        pass
    with kernel(state, work):
        pass


@pytest.mark.fr("FR-035")
async def test_aclose_releases_lock(h):
    from moeka import Kernel

    state, work = dirs(h)
    k1 = Kernel(_mi.make_env(state, work))
    await k1.aclose()
    k2 = Kernel(_mi.make_env(state, work))
    await k2.aclose()


@pytest.mark.fr("FR-035", "SC-006")
@pytest.mark.timeout(120)
def test_sigkill_holder_releases_within_2s(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    ready = h.aux / "ready.json"
    p = h.python("kernel_hold", str(state), str(work), str(ready))
    assert wait_for(ready.exists, 60)
    with pytest.raises(InstanceLockedError):
        kernel(state, work)
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    t0 = time.monotonic()
    k = None
    while time.monotonic() - t0 < 2:
        try:
            k = kernel(state, work)
            break
        except InstanceLockedError:
            time.sleep(0.05)
    assert k is not None, "new writer could not acquire within 2 s of SIGKILL"
    k.close()


# -- FR-036 reason ------------------------------------------------------------------------------


@pytest.mark.fr("FR-036")
def test_refusal_reason_is_held(h):
    from moeka.errors import InstanceLockedError

    state, work = dirs(h)
    ready = h.aux / "r.json"
    h.python("hold_flock", str(state / ".instance.lock"), str(ready))
    assert wait_for(ready.exists, 20)
    with pytest.raises(InstanceLockedError) as info:
        kernel(state, work)
    assert info.value.reason == "held"
    assert info.value.holder is None or isinstance(info.value.holder, dict)


# -- FR-037 / FR-038 read-only attach -----------------------------------------------------------


def _populate(state: Path, work: Path) -> None:
    k = kernel(state, work)
    try:
        s = k.sessions.create_sync("s1")
        s.append_sync({"role": "user", "content": "hello"},
                      {"role": "assistant", "content": "hi"})
        s.set_metadata_sync(topic="t")
        mem = k.memory("notes")
        mem.add("the quick brown fox jumps", source="doc1")
        mem.set_meta("k", "v")
        _mi.attach_fake(k)
        k.llm.complete_sync("count me")
    finally:
        k.close()


@pytest.mark.fr("FR-037")
def test_read_only_takes_no_lock_and_writes_nothing(h):
    state, work = dirs(h)
    _populate(state, work)
    before = tree(state, skip_sidecars=True)
    ro = kernel(state, work, attach="read_only")
    try:
        assert not flock_held(state / ".instance.lock")
        ro.sessions.list()
        ro.sessions.get("s1")
        ro.usage.total()
        ro.memory("notes").count()
    finally:
        ro.close()
    assert tree(state, skip_sidecars=True) == before


@pytest.mark.fr("FR-037")
def test_writer_may_start_while_read_only_attached(h):
    state, work = dirs(h)
    _populate(state, work)
    ro = kernel(state, work, attach="read_only")
    try:
        w = kernel(state, h.aux / "w2")
        w.close()
        assert ro.sessions.get("s1") is not None
    finally:
        ro.close()


@pytest.mark.fr("FR-037")
def test_read_only_while_writer_runs(h):
    state, work = dirs(h)
    _populate(state, work)
    w = kernel(state, work)
    try:
        ro = kernel(state, work, attach="read_only")
        assert [i for i in ro.sessions.list()]
        ro.close()
    finally:
        w.close()


@pytest.mark.fr("FR-037")
def test_read_only_missing_state_dir(h):
    from moeka.errors import ReadOnlyKernelError

    state, work = dirs(h)
    with pytest.raises(ReadOnlyKernelError):
        kernel(state, work, attach="read_only")
    assert not state.exists()


@pytest.mark.fr("FR-037")
@pytest.mark.parametrize("value", ["readonly", "rw", "", "WRITE", "read-only"])
def test_bad_attach_value(h, value):
    state, work = dirs(h)
    with pytest.raises(ValueError):
        kernel(state, work, attach=value)


@pytest.mark.fr("FR-038")
def test_read_only_reads_work(h):
    state, work = dirs(h)
    _populate(state, work)
    with kernel(state, work, attach="read_only") as ro:
        total = ro.usage.total()
        assert total.requests >= 1
        ro.usage.totals(["consumer"])
        ro.usage.records(limit=5)
        ro.usage.budget()
        ro.usage.loss()
        cp = ro.usage.checkpoint()
        assert ro.usage.verify(cp)["ok"] is True
        keys = [getattr(i, "key", None) for i in ro.sessions.list()]
        assert "s1" in keys
        s = ro.sessions.get("s1")
        assert s is not None and len(s.messages) == 2
        assert s.metadata.get("topic") == "t"
        s.snapshot()
        mem = ro.memory("notes")
        assert mem.count() >= 1
        assert mem.search("quick fox", mode="keyword")
        assert "doc1" in mem.sources()
        assert mem.get_meta("k") == "v"


@pytest.mark.fr("FR-038")
def test_read_only_llm_calls_work(h):
    state, work = dirs(h)
    _populate(state, work)
    with kernel(state, work, attach="read_only") as ro:
        _mi.attach_fake(ro)
        before = ro.usage.total().requests
        ro.llm.complete_sync("ro call")
        assert ro.usage.total().requests == before + 1


def _mutations(ro):
    from nanobot.kernel.agent import AgentSpec

    s = ro.sessions.get("s1")
    snap = s.snapshot()
    mem = ro.memory("notes")
    return {
        "agent": lambda: ro.agent(AgentSpec(name="x")),
        "sessions.open": lambda: ro.sessions.open("new-key"),
        "sessions.create_sync": lambda: ro.sessions.create_sync("n2"),
        "sessions.restore_sync": lambda: ro.sessions.restore_sync(snap, key="r2"),
        "sessions.delete_sync": lambda: ro.sessions.delete_sync("s1"),
        "session.append_sync": lambda: s.append_sync({"role": "user", "content": "x"}),
        "session.rewind_sync": lambda: s.rewind_sync(1),
        "session.fork_sync": lambda: s.fork_sync(key="f2"),
        "session.set_metadata_sync": lambda: s.set_metadata_sync(a=1),
        "memory.add": lambda: mem.add("more text"),
        "memory.clear": lambda: mem.clear(),
        "memory.set_meta": lambda: mem.set_meta("k", "w"),
        "memory.new_scope": lambda: ro.memory("never-created-scope"),
        "epistemics.record_fact": lambda: ro.epistemics.record_fact(
            "v", source="user", ref="person:x/y"),
    }


MUTATIONS = ["agent", "sessions.open", "sessions.create_sync", "sessions.restore_sync",
             "sessions.delete_sync", "session.append_sync", "session.rewind_sync",
             "session.fork_sync", "session.set_metadata_sync", "memory.add", "memory.clear",
             "memory.set_meta", "memory.new_scope", "epistemics.record_fact"]


@pytest.mark.fr("FR-038")
@pytest.mark.parametrize("op", MUTATIONS)
def test_read_only_mutations_refused_before_writing(h, op):
    from moeka.errors import ReadOnlyKernelError

    state, work = dirs(h)
    _populate(state, work)
    with kernel(state, work, attach="read_only") as ro:
        ops = _mutations(ro)
        before = tree(state, skip_sidecars=True)
        with pytest.raises(ReadOnlyKernelError):
            ops[op]()
        assert tree(state, skip_sidecars=True) == before
        s = ro.sessions.get("s1")
        assert len(s.messages) == 2


# -- CLI writers (FR-033/034 via nanobot gateway|serve|agent) ------------------------------------


def _instance_cfg(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "config.json"
    p.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(root)}},
        "gateway": {"host": "127.0.0.1", "port": free_port()},
        "api": {"host": "127.0.0.1", "port": free_port()},
        "channels": {"websocket": {"unixSocketPath": str(root / "run" / "ws.sock")}},
    }))
    return p


CLI = {
    "gateway": ["gateway"],
    "serve": ["serve"],
    "agent-m": ["agent", "-m", "hello"],
    "agent-classic": ["agent", "--classic"],
}


@pytest.mark.fr("FR-033", "FR-034")
@pytest.mark.timeout(180)
@pytest.mark.parametrize("cmd", list(CLI), ids=list(CLI))
def test_cli_writer_refused_with_exit_3(h, cmd):
    root = h.aux / "inst"
    cfg = _instance_cfg(root)
    ready = h.aux / "ready.json"
    h.python("hold_flock", str(root / ".instance.lock"), str(ready), str(root / ".instance.json"))
    assert wait_for(ready.exists, 20)
    holder = _read_json(ready)["pid"]
    sibling = root.parent / f"{root.name}-sessions"
    before = tree(root, sibling)
    t0 = time.monotonic()
    r = h.nanobot_cli(*CLI[cmd], "--config", str(cfg), timeout=60)
    assert r.returncode == 3, (cmd, r.returncode, r.stderr[-2000:])
    assert str(root.resolve()) in r.stderr and f"pid {holder}" in r.stderr
    assert time.monotonic() - t0 < 60
    assert tree(root, sibling) == before


@pytest.mark.fr("FR-033")
@pytest.mark.timeout(120)
def test_gateway_lock_blocks_kernel_on_same_root(h):
    """The CLI gateway's lock is the same lock a Kernel takes on that state dir."""
    from moeka.errors import InstanceLockedError

    root = h.aux / "inst"
    _instance_cfg(root)
    ready = h.aux / "ready.json"
    h.python("hold_flock", str(root / ".instance.lock"), str(ready))
    assert wait_for(ready.exists, 20)
    with pytest.raises(InstanceLockedError):
        kernel(root, h.aux / "w")


# -- FR-039 read-only commands -------------------------------------------------------------------


@pytest.mark.fr("FR-039")
@pytest.mark.timeout(120)
def test_status_command_works_while_locked(h):
    root = h.aux / "inst"
    cfg = _instance_cfg(root)
    ready = h.aux / "ready.json"
    h.python("hold_flock", str(root / ".instance.lock"), str(ready), str(root / ".instance.json"))
    assert wait_for(ready.exists, 20)
    r = h.nanobot_cli("status", "--config", str(cfg), timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    assert flock_held(root / ".instance.lock")


@pytest.mark.fr("FR-039")
def test_moeka_sh_read_commands_work_while_locked(h):
    root = h.home / ".moeka-a"
    _instance_cfg(root)
    ready = h.aux / "ready.json"
    h.python("hold_flock", str(root / ".instance.lock"), str(ready), str(root / ".instance.json"))
    assert wait_for(ready.exists, 20)
    r = h.moeka("--workspace", str(root), "status", "--json")
    assert r.returncode in (0, 3) and json.loads(r.stdout.strip())["workspace"] == str(root)
    r = h.moeka("list", "--json")
    assert r.returncode == 0
    assert str(root) in [x["workspace"] for x in json.loads(r.stdout.strip())]
    assert h.moeka("--workspace", str(root), "doctor").returncode == 0
    assert h.moeka("--workspace", str(root), "logs").returncode == 0
