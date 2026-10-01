"""Spec 005 FR-034..FR-038: one writer Kernel per state dir; read-only attach."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from moeka import Environment, Kernel, ModelSpec, ProviderSpec
from moeka.errors import InstanceLockedError, ReadOnlyKernelError
from moeka.testing import FakeProvider, reply
from nanobot.kernel.agent import AgentSpec

MAIN = ModelSpec(name="main", model="m", provider="openai", price_in=1.0, price_out=2.0)


def _env(tmp_path: Path, state: str = "state", work: str = "work") -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / state, work_dir=tmp_path / work,
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[MAIN], default_model="main",
    )


def _snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.name.endswith(("-wal", "-shm")):
                continue
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns) if p.is_file() else (0, 0)
    return out


def _populate(tmp_path: Path) -> None:
    with Kernel(_env(tmp_path)) as writer:
        writer.llm.register_provider("main", FakeProvider(default=reply("hi", input_tokens=3,
                                                                        output_tokens=2)), MAIN)
        writer.llm.complete_sync("hello")
        session = writer.sessions.create_sync("s1", metadata={"topic": "x"})
        session.append_sync({"role": "user", "content": "first"})
        store = writer.memory("notes")
        store.add("alpha beta gamma", source="doc1")


def test_second_writer_refused_in_process_and_files_untouched(tmp_path: Path) -> None:
    with Kernel(_env(tmp_path)) as first:
        assert first.read_only is False
        state = tmp_path / "state"
        before = _snapshot(state, first.env.paths.sessions_root)
        with pytest.raises(InstanceLockedError) as info:
            Kernel(_env(tmp_path))
        err = info.value
        assert err.state_dir == state.resolve() and err.reason == "held"
        assert f"pid {os.getpid()}" in str(err) and str(state.resolve()) in str(err)
        assert _snapshot(state, first.env.paths.sessions_root) == before
        (tmp_path / "alias").symlink_to(state)
        with pytest.raises(InstanceLockedError):
            Kernel(_env(tmp_path, state="alias"))
    with Kernel(_env(tmp_path)):  # released by close
        pass


async def test_aclose_releases(tmp_path: Path) -> None:
    kernel = Kernel(_env(tmp_path))
    await kernel.aclose()
    async with Kernel(_env(tmp_path)):
        pass


def test_dropped_kernel_releases_on_gc(tmp_path: Path) -> None:
    import gc

    kernel = Kernel(_env(tmp_path))
    del kernel
    gc.collect()
    Kernel(_env(tmp_path)).close()


_CHILD = textwrap.dedent("""
    import sys
    from moeka import Environment, Kernel, ModelSpec, ProviderSpec
    from moeka.errors import InstanceLockedError
    env = Environment.for_host(
        state_dir=sys.argv[1], work_dir=sys.argv[2], credentials={},
        providers=[ProviderSpec(name="openai")],
        models=[ModelSpec(name="main", model="m", provider="openai")], default_model="main",
    )
    try:
        Kernel(env)
    except InstanceLockedError as exc:
        print("refused", exc.reason, exc.holder and exc.holder.get("pid"), flush=True)
        sys.exit(3)
    print("acquired", flush=True)
""")


def test_second_writer_refused_cross_process(tmp_path: Path) -> None:
    with Kernel(_env(tmp_path)):
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, str(tmp_path / "state"), str(tmp_path / "w2")],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 3, result.stderr
        assert result.stdout.split() == ["refused", "held", str(os.getpid())]


_HOLD = textwrap.dedent("""
    import sys, time
    from moeka import Environment, Kernel, ModelSpec, ProviderSpec
    env = Environment.for_host(
        state_dir=sys.argv[1], work_dir=sys.argv[2], credentials={},
        providers=[ProviderSpec(name="openai")],
        models=[ModelSpec(name="main", model="m", provider="openai")], default_model="main",
    )
    k = Kernel(env)
    print("ready", flush=True)
    time.sleep(120)
""")


def test_sigkilled_writer_frees_the_state_dir(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(tmp_path / "state"), str(tmp_path / "work")],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        with pytest.raises(InstanceLockedError):
            Kernel(_env(tmp_path))
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        deadline = time.monotonic() + 2
        while True:
            try:
                Kernel(_env(tmp_path)).close()
                break
            except InstanceLockedError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_read_only_attach_reads_everything_and_writes_nothing(tmp_path: Path) -> None:
    _populate(tmp_path)
    state = tmp_path / "state"
    with Kernel(_env(tmp_path)) as writer:  # a writer may run while readers attach
        sessions_root = writer.env.paths.sessions_root
        before = _snapshot(state, sessions_root)
        with Kernel(_env(tmp_path), attach="read_only") as ro:
            assert ro.read_only is True
            assert [i.key for i in ro.sessions.list()] == ["s1"]
            session = ro.sessions.get("s1")
            assert session is not None
            assert session.messages[0]["content"] == "first"
            assert session.metadata["topic"] == "x"
            assert session.snapshot().key == "s1"
            assert ro.sessions.get("nope") is None
            assert ro.usage.total().requests == 1
            assert ro.usage.totals() and ro.usage.records()
            assert ro.usage.loss()["write_dropped"] == 0
            assert ro.usage.budget() is None
            store = ro.memory("notes")
            assert store.count() >= 1 and "doc1" in store.sources()
            assert store.search("beta", mode="keyword")
            assert store.get_meta("missing") is None

            refused = [
                lambda: ro.agent(AgentSpec(name="a")),
                lambda: ro.sessions.open("s1"),
                lambda: ro.sessions.create_sync("s2"),
                lambda: ro.sessions.delete_sync("s1"),
                lambda: ro.sessions.restore_sync(session.snapshot(), key="s3"),
                lambda: session.append_sync({"role": "user", "content": "no"}),
                lambda: session.rewind_sync(0),
                lambda: session.fork_sync(),
                lambda: session.set_metadata_sync(a=1),
                lambda: store.add("more"),
                lambda: store.clear(),
                lambda: store.set_meta("k", "v"),
                lambda: ro.memory("not-there"),
                lambda: ro.epistemics.record_fact(1, source="user", ref="x"),
            ]
            for call in refused:
                with pytest.raises(ReadOnlyKernelError):
                    call()
        assert _snapshot(state, sessions_root) == before


async def test_read_only_async_mutations_refused(tmp_path: Path) -> None:
    _populate(tmp_path)
    async with Kernel(_env(tmp_path), attach="read_only") as ro:
        session = ro.sessions.get("s1")
        assert session is not None
        with pytest.raises(ReadOnlyKernelError):
            await session.append({"role": "user", "content": "x"})
        with pytest.raises(ReadOnlyKernelError):
            await ro.sessions.create("s9")


def test_read_only_llm_calls_work(tmp_path: Path) -> None:
    _populate(tmp_path)
    with Kernel(_env(tmp_path), attach="read_only") as ro:
        ro.llm.register_provider("main", FakeProvider(default="pong"), MAIN)
        assert ro.llm.complete_sync("ping").text == "pong"
        assert ro.usage.total().requests == 2


def test_read_only_without_writer_then_writer_starts(tmp_path: Path) -> None:
    _populate(tmp_path)
    with Kernel(_env(tmp_path), attach="read_only") as ro:
        with Kernel(_env(tmp_path)) as writer:
            writer.sessions.get("s1").append_sync({"role": "user", "content": "second"})
            assert len(ro.sessions.get("s1").messages) == 2


def test_read_only_on_missing_state_dir(tmp_path: Path) -> None:
    with pytest.raises(ReadOnlyKernelError):
        Kernel(_env(tmp_path, state="missing"), attach="read_only")
    assert not (tmp_path / "missing").exists()


def test_read_only_on_empty_state_dir_creates_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with Kernel(_env(tmp_path), attach="read_only") as ro:
        assert ro.sessions.list() == []
        assert ro.usage.total().requests == 0
        with pytest.raises(ReadOnlyKernelError):
            ro.memory("x")
    assert list(state.iterdir()) == []
    assert not (tmp_path / "work").exists() or list((tmp_path / "work").iterdir()) == []


def test_bad_attach_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Kernel(_env(tmp_path), attach="exclusive")
    assert not (tmp_path / "state").exists()
