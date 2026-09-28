"""Session handles (Task 10): append, checkpoint, fork, rewind, snapshot, persistence."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from moeka.agents import AgentSpec
from moeka.sessions import (
    Checkpoint,
    CheckpointMismatch,
    Session,
    SessionBusyError,
    SessionInfo,
    Sessions,
    SessionSnapshot,
)
from moeka.testing import FakeProvider
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.session.manager import SessionManager
from nanobot.session.summary import SUMMARY_CONTINUATION_TEXT

MAIN = ModelSpec(name="main", model="fake-main", provider="openai", max_tokens=100)


def _env(tmp_path: Path) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )


@pytest.fixture
def make_kernel(tmp_path):
    kernels: list[Kernel] = []

    def make(fake: FakeProvider | None = None) -> Kernel:
        kernel = Kernel(_env(tmp_path))
        if fake is not None:
            kernel.llm.register_provider("main", fake, MAIN)
        kernels.append(kernel)
        return kernel

    yield make
    for kernel in kernels:
        kernel.close()


def _contents(session: Session) -> list[Any]:
    return [m.get("content") for m in session.messages]


def _sent(fake: FakeProvider) -> list[str]:
    """Plain-text user/assistant contents of the latest provider call."""
    out = []
    for m in fake.calls[-1].messages:
        if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str):
            out.append(m["content"])
    return out


async def _wait_for(predicate) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# -- append / checkpoint -----------------------------------------------------------


async def test_append_host_ordered_with_speaker_names(make_kernel) -> None:
    kernel = make_kernel()
    assert isinstance(kernel.sessions, Sessions)
    s = await kernel.sessions.create("coach", metadata={"scene": "dinner"})
    cp = await s.append(
        {"role": "system", "content": "You are the coach."},
        {"role": "user", "content": "Hi Sam", "name": "alex"},
        {"role": "assistant", "content": "Hey!", "name": "sam"},
    )
    assert isinstance(cp, Checkpoint)
    assert cp.key == "coach" and cp.n_messages == 3
    msgs = s.messages
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert [m.get("name") for m in msgs] == [None, "alex", "sam"]
    assert s.metadata["scene"] == "dinner"
    with pytest.raises(TypeError):
        msgs[0]["content"] = "x"  # type: ignore[index]
    assert s.checkpoint() == cp
    with pytest.raises(ValueError):
        await s.append({"role": "narrator", "content": "x"})
    with pytest.raises(ValueError):
        await s.append({"role": "user"})
    with pytest.raises(ValueError):
        await kernel.sessions.create("coach")
    assert len(s.messages) == 3


async def test_checkpoint_digest_is_prefix_content(make_kernel) -> None:
    kernel = make_kernel()
    s = await kernel.sessions.create()
    assert s.key.startswith("session-")
    cp1 = await s.append({"role": "user", "content": "a"})
    cp2 = await s.append({"role": "assistant", "content": "b"})
    assert cp1.n_messages == 1 and cp2.n_messages == 2
    assert cp1.digest != cp2.digest and len(cp1.digest) == 64
    assert s.checkpoint() == cp2
    # Same content under another key digests the same (a fork's parent checkpoints apply).
    fork = await s.fork(cp1)
    assert fork.checkpoint().digest == cp1.digest


# -- fork -------------------------------------------------------------------------


async def test_fork_divergent_appends_and_runs_leave_parent_unchanged(make_kernel) -> None:
    fake = FakeProvider(default="reply")
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="coach"))
    parent = await kernel.sessions.create("p")
    await parent.append({"role": "user", "content": "one"}, {"role": "assistant", "content": "1"})
    early = parent.checkpoint()
    await parent.append({"role": "user", "content": "two"}, {"role": "assistant", "content": "2"})
    before = parent.messages

    child = await parent.fork(key="p/child")
    assert child.key == "p/child" and child.messages == before
    await child.append({"role": "user", "content": "child only"})
    await agent.run("child run", session=child)
    assert parent.messages == before

    earlier = await parent.fork(early)
    assert earlier.key.startswith("p/fork-")
    assert _contents(earlier) == ["one", "1"]
    result = await agent.run("alt line", session=earlier)
    assert result.session_key == earlier.key
    sent = " ".join(_sent(fake))
    assert "one" in sent and "alt line" in sent
    assert "two" not in sent and "child only" not in sent
    assert parent.messages == before
    assert _contents(earlier)[:2] == ["one", "1"]
    with pytest.raises(ValueError):
        await parent.fork(key="p/child")


# -- rewind -----------------------------------------------------------------------


async def test_rewind_then_run_continues_from_rewound_state(make_kernel) -> None:
    fake = FakeProvider(default="ok")
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="a"))
    s = await kernel.sessions.create("r")
    cp = await s.append({"role": "user", "content": "setup"},
                        {"role": "assistant", "content": "ready"})
    await agent.run("first attempt", session=s)
    assert len(s.messages) > 2
    await s.rewind(cp)
    assert _contents(s) == ["setup", "ready"]
    await agent.run("second attempt", session=s)
    sent = " ".join(_sent(fake))
    assert "setup" in sent and "second attempt" in sent
    assert "first attempt" not in sent
    await s.rewind(0)
    assert s.messages == ()
    with pytest.raises(ValueError):
        await s.rewind(5)


async def test_rewind_fork_delete_refused_during_run(make_kernel) -> None:
    fake = FakeProvider(["late"], delay=0.5)
    kernel = make_kernel(fake)
    s = await kernel.sessions.create("busy")
    cp = await s.append({"role": "user", "content": "x"})
    task = asyncio.create_task(kernel.agent(AgentSpec(name="a")).run("go", session=s))
    await _wait_for(lambda: fake.calls)
    with pytest.raises(SessionBusyError):
        await s.rewind(cp)
    with pytest.raises(SessionBusyError):
        await s.fork()
    with pytest.raises(SessionBusyError):
        await kernel.sessions.delete("busy")
    with pytest.raises(SessionBusyError):
        await s.append({"role": "user", "content": "y"}, timeout=0.01)
    # append without a timeout waits for the run, then lands after it.
    cp2 = await s.append({"role": "user", "content": "after"})
    result = await task
    assert result.content == "late"
    assert _contents(s)[-1] == "after" and cp2.n_messages == len(s.messages)
    await s.rewind(cp)
    assert _contents(s) == ["x"]


async def test_checkpoint_mismatch_after_divergent_rewind_and_append(make_kernel) -> None:
    kernel = make_kernel()
    s = await kernel.sessions.create()
    base = await s.append({"role": "user", "content": "a"})
    later = await s.append({"role": "assistant", "content": "b"},
                           {"role": "user", "content": "c"})
    await s.rewind(base)
    await s.append({"role": "assistant", "content": "B"}, {"role": "user", "content": "C"})
    with pytest.raises(CheckpointMismatch):
        await s.rewind(later)
    with pytest.raises(CheckpointMismatch):
        await s.fork(later)
    await s.rewind(base)  # the shared prefix still matches
    with pytest.raises(CheckpointMismatch):
        await s.rewind(later)  # now beyond the end


# -- concurrency / persistence / snapshot -------------------------------------------


async def test_fifty_concurrent_sessions(make_kernel) -> None:
    fake = FakeProvider(default="ok", delay=0.01)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="many"))

    async def one(i: int) -> Session:
        s = await kernel.sessions.create(f"s{i}", metadata={"i": i})
        await s.append({"role": "user", "content": f"hello {i}", "name": f"u{i}"})
        await asyncio.gather(
            agent.run(f"run {i}", session=s),
            s.append({"role": "assistant", "content": f"note {i}"}),
        )
        return s

    sessions = await asyncio.gather(*(one(i) for i in range(50)))
    for i, s in enumerate(sessions):
        text = " ".join(str(c) for c in _contents(s))
        assert f"hello {i}" in text and f"run {i}" in text and f"note {i}" in text
        hellos = {c for c in _contents(s) if isinstance(c, str) and c.startswith("hello")}
        assert hellos == {f"hello {i}"}
    infos = {info.key: info for info in kernel.sessions.list()}
    assert all(f"s{i}" in infos for i in range(50))
    assert isinstance(infos["s7"], SessionInfo) and infos["s7"].metadata["i"] == 7


async def test_sessions_survive_kernel_restart(tmp_path) -> None:
    k1 = Kernel(_env(tmp_path))
    s = await k1.sessions.create("keep", metadata={"who": "alex"})
    await s.append({"role": "user", "content": "persist me", "name": "alex"})
    await s.set_metadata(stage=2)
    fork = await s.fork(key="keep/f")
    cp = s.checkpoint()
    await k1.aclose()

    k2 = Kernel(_env(tmp_path))
    try:
        again = k2.sessions.get("keep")
        assert again is not None
        assert _contents(again) == ["persist me"]
        assert again.messages[0]["name"] == "alex"
        assert dict(again.metadata) == {"who": "alex", "stage": 2}
        assert k2.sessions.get(fork.key) is not None
        assert k2.sessions.get("missing") is None
        assert {i.key for i in k2.sessions.list()} >= {"keep", "keep/f"}
        assert again.checkpoint() == cp
    finally:
        await k2.aclose()


async def test_snapshot_restore_round_trip(make_kernel) -> None:
    kernel = make_kernel()
    s = await kernel.sessions.create("orig", metadata={"k": [1, 2]})
    await s.append({"role": "user", "content": "q", "name": "a"},
                   {"role": "assistant", "content": "r"})
    snap = s.snapshot()
    assert isinstance(snap, SessionSnapshot)
    data = snap.to_dict()
    restored = await kernel.sessions.restore(SessionSnapshot(**data), key="copy")
    assert restored.messages == s.messages
    assert restored.checkpoint().digest == s.checkpoint().digest
    assert dict(restored.metadata) == dict(s.metadata)
    with pytest.raises(ValueError):
        await kernel.sessions.restore(snap)  # "orig" exists
    assert await kernel.sessions.delete("copy") is True
    assert await kernel.sessions.delete("copy") is False
    assert kernel.sessions.get("copy") is None


# -- consolidation state -------------------------------------------------------------


def _seed_consolidated(manager: SessionManager, key: str) -> None:
    session = manager.get_or_create(key)
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": SUMMARY_CONTINUATION_TEXT, HIDDEN_HISTORY_META: True},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    session.last_archived = 2
    session.metadata["_last_summary"] = {"text": "u1/a1", "last_active": "2026-01-01T00:00:00"}
    session.metadata["_last_usage"] = {"prompt_tokens": 99}
    session.metadata["runtime_checkpoint"] = {"phase": "tools"}
    session.metadata["pending_user_turn"] = {"content": "x"}
    session.metadata["title"] = "kept"
    manager.save(session)


def test_truncate_invalidates_consolidation_state(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    _seed_consolidated(manager, "k")
    kept = manager.truncate("k", 4)
    assert kept is not None and len(kept.messages) == 4
    assert kept.last_archived == 2 and "_last_summary" in kept.metadata
    assert "_last_usage" not in kept.metadata
    assert "runtime_checkpoint" not in kept.metadata
    assert "pending_user_turn" not in kept.metadata
    assert kept.metadata["title"] == "kept"

    cut = manager.truncate("k", 1)
    assert cut is kept and cut.last_archived == 0
    assert "_last_summary" not in cut.metadata
    reloaded = SessionManager(tmp_path).read_session_snapshot("k")
    assert reloaded is not None and reloaded.last_archived == 0
    assert len(reloaded.messages) == 1 and "_last_summary" not in reloaded.metadata
    assert manager.truncate("missing", 0) is None
    with pytest.raises(ValueError):
        manager.truncate("k", 2)


def test_fork_session_cuts_consolidation_state(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    _seed_consolidated(manager, "k")
    whole = manager.fork_session("k", "f1", upto_message=5, strip_runtime_context=False)
    assert whole is not None and whole.last_archived == 2
    assert "_last_summary" in whole.metadata and "_last_usage" in whole.metadata
    assert "runtime_checkpoint" not in whole.metadata
    short = manager.fork_session("k", "f2", upto_message=1)
    assert short is not None and short.last_archived == 0
    assert "_last_summary" not in short.metadata and "_last_usage" not in short.metadata
    assert manager.fork_session("k", "f3", upto_message=6) is None
    assert manager.fork_session("nope", "f4", upto_message=0) is None


async def test_kernel_rewind_and_fork_cut_consolidation(make_kernel) -> None:
    kernel = make_kernel()
    await kernel.sessions.create("c")
    _seed_consolidated(kernel._session_manager(), "c")
    s = kernel.sessions.get("c")
    assert s is not None
    fork = await s.fork(1)
    stored = kernel._session_manager().get_or_create(fork.key)
    assert stored.last_archived == 0 and "_last_summary" not in stored.metadata
    assert fork.metadata["title"] == "kept"  # host metadata survives a kernel fork
    await s.rewind(4)
    stored = kernel._session_manager().get_or_create("c")
    assert stored.last_archived == 2 and "runtime_checkpoint" not in stored.metadata
    # A restored snapshot recovers the summary offset from its marker.
    restored = await kernel.sessions.restore(s.snapshot(), key="c2")
    assert kernel._session_manager().get_or_create(restored.key).last_archived == 2


# -- sync twins ----------------------------------------------------------------------


def test_sync_twins_from_a_thread(make_kernel) -> None:
    fake = FakeProvider(default="ok")
    kernel = make_kernel(fake)
    errors: list[BaseException] = []
    seen: dict[str, Any] = {}

    def work() -> None:
        try:
            s = kernel.sessions.create_sync("t", metadata={"a": 1})
            cp = s.append_sync({"role": "user", "content": "hi", "name": "x"})
            s.append_sync({"role": "assistant", "content": "yo"})
            f = s.fork_sync(cp, key="t/f")
            s.rewind_sync(cp)
            s.set_metadata_sync(b=2)
            kernel.agent(AgentSpec(name="a")).run_sync("more", session=f)
            r = kernel.sessions.restore_sync(s.snapshot(), key="t/r")
            seen.update(
                s=_contents(s), f=_contents(f), r=_contents(r), meta=dict(s.metadata),
                opened=kernel.sessions.open("t/new").messages,
                deleted=kernel.sessions.delete_sync("t/r"),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    assert not errors, errors
    assert seen["s"] == ["hi"] and seen["r"] == ["hi"]
    assert seen["f"][0] == "hi" and "more" in " ".join(map(str, seen["f"]))
    assert seen["meta"] == {"a": 1, "b": 2}
    assert seen["opened"] == () and seen["deleted"] is True
    assert kernel.sessions.get("t/new") is not None


def test_session_from_another_kernel_is_rejected(make_kernel, tmp_path) -> None:
    k1 = make_kernel(FakeProvider(default="ok"))
    other = Kernel(_env(tmp_path / "other"))
    try:
        s = other.sessions.open("x")
        with pytest.raises(ValueError):
            k1.agent(AgentSpec(name="a")).run_sync("hi", session=s)
        with pytest.raises(TypeError):
            k1.agent(AgentSpec(name="a")).run_sync("hi", session=42)  # type: ignore[arg-type]
    finally:
        other.close()


def test_closed_kernel_refuses_sessions(tmp_path) -> None:
    kernel = Kernel(_env(tmp_path))
    kernel.close()
    with pytest.raises(RuntimeError):
        kernel.sessions  # noqa: B018
