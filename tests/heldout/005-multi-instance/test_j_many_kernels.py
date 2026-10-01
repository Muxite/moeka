"""Group J: many Kernels and agent cores (FR-053..FR-055, US4)."""

from __future__ import annotations

from pathlib import Path

import pytest

import _mi


def _kernel(state, work, **kw):
    from moeka import Kernel

    data_dir = kw.pop("data_dir", None)
    trace = kw.pop("trace", None)
    return Kernel(_mi.make_env(state, work, data_dir=data_dir, trace=trace), **kw)


@pytest.mark.fr("FR-053")
def test_for_host_data_dir(h):
    d = h.aux / "shared-data"
    env = _mi.make_env(h.aux / "s", h.aux / "w", data_dir=str(d))
    assert env.paths.data_dir == d.resolve()
    env2 = _mi.make_env(h.aux / "s2", h.aux / "w2")
    assert env2.paths.data_dir == (h.aux / "s2").resolve() / "data"
    with _kernel(h.aux / "s3", h.aux / "w3", data_dir=d) as k:
        _mi.attach_fake(k)
        k.llm.complete_sync("x")
    assert (d / "llm_usage.sqlite3").exists()
    assert not (h.aux / "s3" / "data" / "llm_usage.sqlite3").exists()


@pytest.mark.fr("FR-053")
def test_data_dir_inside_work_dir_refused_under_strict(h):
    from moeka import PathsOverlapError

    w = h.aux / "w"
    for bad in (w, w / "data", w / "a" / "b"):
        with pytest.raises(PathsOverlapError):
            _mi.make_env(h.aux / "s", w, data_dir=bad)
    env = _mi.make_env(h.aux / "s", w, data_dir=w / "data", strict=False)
    assert env.paths.data_dir == (w / "data").resolve()


@pytest.mark.fr("FR-054")
def test_two_writer_kernels_one_process(h):
    d = h.aux / "data"
    s1, s2 = h.aux / "s1", h.aux / "s2"
    k1 = _kernel(s1, h.aux / "w1", data_dir=d, consumer="c1")
    k2 = _kernel(s2, h.aux / "w2", data_dir=d, consumer="c2")
    try:
        k1.sessions.create_sync("only-in-1").append_sync({"role": "user", "content": "1"})
        k2.sessions.create_sync("only-in-2").append_sync({"role": "user", "content": "2"})
        keys1 = {i.key for i in k1.sessions.list()}
        keys2 = {i.key for i in k2.sessions.list()}
        assert "only-in-1" in keys1 and "only-in-1" not in keys2
        assert "only-in-2" in keys2 and "only-in-2" not in keys1
        k1.memory("notes").add("alpha bravo charlie", source="k1doc")
        assert k2.memory("notes").count() == 0
        _mi.attach_fake(k1)
        _mi.attach_fake(k2)
        k1.llm.complete_sync("x")
    finally:
        k1.close()
    try:
        k2.llm.complete_sync("y")
        k2.sessions.get("only-in-2").append_sync({"role": "assistant", "content": "ok"})
        assert len(k2.sessions.get("only-in-2").messages) == 2
        assert k2.memory("notes").count() == 0
        assert k2.usage.total().requests == 2
        assert k2.usage.total(consumer="c2").requests == 1
    finally:
        k2.close()


@pytest.mark.fr("FR-055")
def test_distinct_memory_keys(h):
    from nanobot.kernel.agent import AgentSpec

    s = h.aux / "s"
    with _kernel(s, h.aux / "w") as k:
        _mi.attach_fake(k)
        a = k.agent(AgentSpec(name="alpha"))
        b = k.agent(AgentSpec(name="bravo", memory_key="bravo-mem"))
        a.run_sync("hello")
        b.run_sync("hello")
        keys = {i.key for i in k.sessions.list()}
        assert "agent:alpha" in keys and "agent:bravo-mem" in keys
    ma = s / "agents" / "alpha" / "memory"
    mb = s / "agents" / "bravo-mem" / "memory"
    assert ma.is_dir() and mb.is_dir()
    fa = {p.resolve() for p in ma.rglob("*")}
    fb = {p.resolve() for p in mb.rglob("*")}
    assert not fa & fb


@pytest.mark.fr("FR-055")
def test_shared_memory_key_emits_trace_event(h):
    from nanobot.kernel.agent import AgentSpec

    sink = _mi.Sink()
    with _kernel(h.aux / "s", h.aux / "w", trace=sink) as k:
        _mi.attach_fake(k)
        k.agent(AgentSpec(name="writer", memory_key="shared"))
        assert not sink.named("kernel.memory_key_shared")
        second = k.agent(AgentSpec(name="reviewer", memory_key="shared"))
        assert second is not None  # a warning, not a refusal
        events = sink.named("kernel.memory_key_shared")
        assert len(events) == 1
        assert events[0]["memory_key"] == "shared"
        assert set(events[0]["agents"]) == {"writer", "reviewer"}
        k.agent(AgentSpec(name="third", memory_key="other"))
        assert len(sink.named("kernel.memory_key_shared")) == 1
