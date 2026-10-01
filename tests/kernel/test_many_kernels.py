"""Spec 005 FR-052..FR-055: many Kernels per process and per data dir; agent memory keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from moeka import Environment, Kernel, ModelSpec, ProviderSpec
from moeka.testing import FakeProvider, reply
from nanobot.kernel.agent import AgentSpec
from nanobot.kernel.env import PathsOverlapError
from nanobot.kernel.trace import MemoryTraceSink

MAIN = ModelSpec(name="main", model="m", provider="openai", price_in=1.0, price_out=2.0)


def _env(tmp_path: Path, state: str, *, data: Path | None = None, sink=None,
         work: str | None = None, strict: bool = True) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / state, work_dir=tmp_path / (work or f"{state}-work"),
        data_dir=data, credentials={"oa": "x"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[MAIN], default_model="main", trace=sink, strict=strict,
    )


def _fake(kernel: Kernel) -> FakeProvider:
    fake = FakeProvider(default=reply("ok", input_tokens=4, output_tokens=2))
    kernel.llm.register_provider("main", fake, MAIN)
    return fake


def test_for_host_data_dir(tmp_path: Path) -> None:
    env = _env(tmp_path, "s", data=tmp_path / "shared" / ".." / "shared")
    assert env.paths.data_dir == (tmp_path / "shared").resolve()
    assert _env(tmp_path, "t").paths.data_dir == (tmp_path / "t" / "data").resolve()
    with pytest.raises(PathsOverlapError):
        _env(tmp_path, "u", data=tmp_path / "u-work")
    with pytest.raises(PathsOverlapError):
        _env(tmp_path, "v", data=tmp_path / "v-work" / "data")
    _env(tmp_path, "w", data=tmp_path / "w-work" / "data", strict=False)


def test_two_kernels_share_a_data_dir_with_exact_per_consumer_totals(tmp_path: Path) -> None:
    shared = tmp_path / "data"
    a = Kernel(_env(tmp_path, "a", data=shared), consumer="app-a")
    b = Kernel(_env(tmp_path, "b", data=shared), consumer="app-b")
    try:
        _fake(a)
        _fake(b)
        for _ in range(3):
            a.llm.complete_sync("x")
        for _ in range(5):
            b.llm.complete_sync("y")
        assert (shared / "llm_usage.sqlite3").exists()
        for kernel in (a, b):
            assert kernel.usage.total().requests == 8
            assert kernel.usage.total(consumer="app-a").requests == 3
            assert kernel.usage.total(consumer="app-b").requests == 5
            grouped = kernel.usage.totals(["consumer"])
            assert sum(g.requests for g in grouped) == kernel.usage.total().requests
        a.close()
        b.llm.complete_sync("z")  # the other kernel and the shared store keep working
        assert b.usage.total().requests == 9
        session = b.sessions.create_sync("after-close")
        session.append_sync({"role": "user", "content": "still here"})
        assert b.sessions.get("after-close") is not None
    finally:
        a.close()
        b.close()


def test_kernels_keep_their_state_under_their_own_state_dir(tmp_path: Path) -> None:
    with Kernel(_env(tmp_path, "a")) as a, Kernel(_env(tmp_path, "b")) as b:
        a.sessions.create_sync("only-a")
        b.memory("notes").add("text for b", source="b")
        assert b.sessions.get("only-a") is None
        assert not (tmp_path / "a" / "memory" / "notes.db").exists()
        assert (tmp_path / "b" / "memory" / "notes.db").exists()


def test_distinct_memory_keys_use_distinct_dirs(tmp_path: Path) -> None:
    sink = MemoryTraceSink()
    with Kernel(_env(tmp_path, "s", sink=sink)) as kernel:
        _fake(kernel)
        one = kernel.agent(AgentSpec(name="one", memory_key="k1"))
        two = kernel.agent(AgentSpec(name="two", memory_key="k2"))
        one.run_sync("hello")
        two.run_sync("hello")
        agents = tmp_path / "s" / "agents"
        assert (agents / "k1" / "memory").is_dir() and (agents / "k2" / "memory").is_dir()
        f1 = {p.relative_to(agents / "k1") for p in (agents / "k1").rglob("*")}
        f2 = {p.relative_to(agents / "k2") for p in (agents / "k2").rglob("*")}
        assert f1 and f2
        keys = {i.key for i in kernel.sessions.list()}
        assert {"agent:k1", "agent:k2"} <= keys
        assert not [e for e in sink.events if e["event"] == "kernel.memory_key_shared"]


def test_shared_memory_key_emits_trace_event(tmp_path: Path) -> None:
    sink = MemoryTraceSink()
    with Kernel(_env(tmp_path, "s", sink=sink)) as kernel:
        _fake(kernel)
        kernel.agent(AgentSpec(name="alpha", memory_key="same"))
        kernel.agent(AgentSpec(name="beta", memory_key="same"))
    events = [e for e in sink.events if e["event"] == "kernel.memory_key_shared"]
    assert len(events) == 1
    assert events[0]["memory_key"] == "same"
    assert sorted(events[0]["agents"]) == ["alpha", "beta"]
