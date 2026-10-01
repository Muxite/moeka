"""K4: ``RunLimits.max_tool_errors`` (FR-020 to FR-026, SC-004, US2)."""

from __future__ import annotations

import asyncio

import pytest
from _h006 import calls, ev, tc, tools_of

from moeka.agents import AgentSpec, RunLimits, RunResult
from moeka.testing import FakeProvider
from moeka.tools import DefaultPolicy

UNKNOWN = "no_such_tool_h006"


def boom_h006(q: str) -> str:
    raise RuntimeError("boom")


def _always_unknown(fake_ref: list) -> object:
    def item(call):
        n = len(fake_ref[0].calls)
        return tc(UNKNOWN, {"n": n}, f"u{n}")
    return item


def _looping_fake() -> FakeProvider:
    ref: list = []
    fake = FakeProvider(default=_always_unknown(ref))
    ref.append(fake)
    return fake


def _agent(kmaker, fake, **kw):
    kernel = kmaker(fake)
    return kernel.agent(AgentSpec(name=kw.pop("name", "k4"), **kw))


def _msg(count: int, limit: int) -> str:
    return f"max_tool_errors: {count} tool errors (limit {limit})"


# -- FR-020: construction ------------------------------------------------------------------


@pytest.mark.fr("FR-020")
@pytest.mark.parametrize("n", [1, 2, 3, 50, 10_000])
def test_positive_limits_build(kmaker, n):
    agent = _agent(kmaker, FakeProvider(default="ok"), limits=RunLimits(max_tool_errors=n))
    assert tools_of(agent)
    agent.fingerprint()


@pytest.mark.fr("FR-020")
@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "3", 3.0])
def test_invalid_limits_raise_value_error(bad):
    with pytest.raises(ValueError):
        RunLimits(max_tool_errors=bad)


# -- SC-004 / FR-023 / FR-025: the ceiling --------------------------------------------------


@pytest.mark.fr("SC-004", "FR-023", "FR-025", "FR-021")
def test_sc004_three_errors_three_calls(kmaker, sink):
    """US2 scenario 1."""
    fake = _looping_fake()
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=3, max_iterations=20))
    result = agent.run_sync("go")
    assert isinstance(result, RunResult)
    assert len(fake.calls) == 3
    assert result.stop_reason == "tool_error"
    assert result.error == _msg(3, 3)
    assert result.iterations == 3
    done = ev(sink, "run.completed")
    assert len(done) == 1 and done[0]["stop_reason"] == "tool_error"
    assert sum(1 for e in ev(sink, "tool.call") if e["ok"] is False) == 3


@pytest.mark.fr("SC-004", "FR-026")
def test_sc004_none_never_tool_error(kmaker, sink):
    fake = _looping_fake()
    agent = _agent(kmaker, fake, limits=RunLimits(max_iterations=6))
    result = agent.run_sync("go")
    assert result.stop_reason != "tool_error"
    assert all(e["stop_reason"] != "tool_error" for e in ev(sink, "run.completed"))
    assert len(fake.calls) >= 6


@pytest.mark.fr("FR-023", "FR-025")
def test_all_calls_of_the_response_execute(kmaker, sink):
    script = [calls((UNKNOWN, {"i": 1}, "a1"), (UNKNOWN, {"i": 2}, "a2"),
                    (UNKNOWN, {"i": 3}, "a3"), ("list_dir", {"path": "."}, "a4"))]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=2))
    result = agent.run_sync("go")
    assert result.stop_reason == "tool_error"
    assert len(fake.calls) == 1 and result.iterations == 1
    assert [e["call_id"] for e in ev(sink, "tool.call")] == ["a1", "a2", "a3", "a4"]
    assert result.error == _msg(3, 2)


@pytest.mark.fr("FR-023", "FR-021")
def test_errors_spread_over_iterations(kmaker, sink):
    script = [
        calls((UNKNOWN, {}, "b1"), ("list_dir", {"path": "."}, "b2")),
        calls(("list_dir", {"path": "."}, "b3")),
        calls((UNKNOWN, {"x": 1}, "b4"), (UNKNOWN, {"x": 2}, "b5")),
    ]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=3))
    result = agent.run_sync("go")
    assert result.stop_reason == "tool_error"
    assert len(fake.calls) == 3 and result.iterations == 3
    assert result.error == _msg(3, 3)


@pytest.mark.fr("FR-021", "FR-023")
def test_every_failure_kind_counts(kmaker, sink, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    script = [calls(
        (UNKNOWN, {}, "k1"),                              # invalid_args (unknown tool)
        ("read_file", {"path": "a.txt", "offset": 0}, "k2"),              # invalid_args (validation)
        ("read_file", {"path": "missing-h006.txt"}, "k3"),  # error result
        ("boom_h006", {"q": "x"}, "k4"),                  # tool exception
        ("list_dir", {"path": "."}, "k5"),                # success: not counted
    )]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, actions=(boom_h006,), limits=RunLimits(max_tool_errors=4))
    result = agent.run_sync("go")
    assert result.stop_reason == "tool_error"
    assert len(fake.calls) == 1
    assert result.error == _msg(4, 4)
    bad = [e for e in ev(sink, "tool.call") if e["ok"] is False]
    assert len(bad) == 4


@pytest.mark.fr("FR-021", "FR-026")
def test_successes_do_not_count(kmaker, sink):
    script = [calls(*[("list_dir", {"path": "."}, f"s{i}") for i in range(5)]), "done"]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=1))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and result.content == "done"


@pytest.mark.fr("FR-026")
def test_n_minus_one_errors_then_complete(kmaker, sink):
    script = [tc(UNKNOWN, {}, "e1"), tc(UNKNOWN, {"x": 1}, "e2"), "finished"]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=3))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and result.content == "finished"
    assert result.error is None
    assert ev(sink, "run.completed")[0]["stop_reason"] == "completed"


@pytest.mark.fr("FR-026")
def test_none_tolerates_many_errors(kmaker):
    script = [calls(*[(UNKNOWN, {"i": i}, f"m{i}") for i in range(8)]), "finished"]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake)
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and result.content == "finished"


# -- FR-022: per run -------------------------------------------------------------------------


@pytest.mark.fr("FR-022")
def test_count_is_per_run_same_session(kmaker, sink):
    script = [tc(UNKNOWN, {}, "p1"), "first done", tc(UNKNOWN, {}, "p2"), "second done",
              tc(UNKNOWN, {}, "p3"), "third done"]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=2))
    for expected in ("first done", "second done", "third done"):
        result = agent.run_sync("go", session="shared-h006")
        assert result.stop_reason == "completed" and result.content == expected
    assert len(fake.calls) == 6


@pytest.mark.fr("FR-022")
def test_count_resets_after_a_tool_error_stop(kmaker, sink):
    script = [tc(UNKNOWN, {}, "q1"), tc(UNKNOWN, {}, "q2"), "after"]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=1))
    first = agent.run_sync("go", session="s-h006")
    assert first.stop_reason == "tool_error" and first.error == _msg(1, 1)
    second = agent.run_sync("again", session="s-h006")
    assert second.stop_reason == "tool_error" and second.error == _msg(1, 1)
    third = agent.run_sync("more", session="s-h006")
    assert third.stop_reason == "completed" and third.content == "after"


# -- FR-024: precedence ---------------------------------------------------------------------


@pytest.mark.fr("FR-024")
def test_ask_user_wins_over_tool_error(kmaker, sink):
    script = [calls((UNKNOWN, {}, "x1"), (UNKNOWN, {"b": 1}, "x2"),
                    ("ask_user", {"question": "which one?"}, "x3"))]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=1))
    result = agent.run_sync("go")
    assert result.stop_reason == "ask_user"
    assert result.question is not None and "which one?" in result.question.question
    assert len(fake.calls) == 1


@pytest.mark.fr("FR-024", "FR-021")
def test_ask_user_alone_is_not_a_tool_error(kmaker, sink):
    fake = FakeProvider([tc("ask_user", {"question": "ok?"}, "y1")], default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=1))
    result = agent.run_sync("go")
    assert result.stop_reason == "ask_user"


@pytest.mark.fr("FR-024")
def test_policy_denials_win_over_tool_error(kmaker, sink, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "secret-h006.txt").write_text("s")
    script = [calls((UNKNOWN, {}, "d1"), ("read_file", {"path": "secret-h006.txt"}, "d2"))]
    fake = FakeProvider(script, default="closing")
    agent = _agent(
        kmaker, fake, policy=DefaultPolicy(deny_rules=(("fs.read", "*secret-h006*"),)),
        limits=RunLimits(max_tool_errors=1, max_policy_denials=1),
    )
    result = agent.run_sync("go")
    assert result.stop_reason == "policy_denials"
    assert ev(sink, "run.completed")[0]["stop_reason"] == "policy_denials"


@pytest.mark.fr("FR-021")
def test_gate_denials_are_not_tool_errors(kmaker, sink, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "secret-h006.txt").write_text("s")
    script = [calls(("read_file", {"path": "secret-h006.txt"}, "g1"),
                    ("read_file", {"path": "./secret-h006.txt"}, "g2")), "done"]
    fake = FakeProvider(script, default="never")
    agent = _agent(
        kmaker, fake, policy=DefaultPolicy(deny_rules=(("fs.read", "*secret-h006*"),)),
        limits=RunLimits(max_tool_errors=1, max_policy_denials=10),
    )
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and result.content == "done"
    assert [e for e in ev(sink, "tool.call") if e["call_id"] in ("g1", "g2")] == []


@pytest.mark.fr("FR-024")
def test_tool_error_before_iteration_budget(kmaker, sink):
    script = [tc(UNKNOWN, {}, "i1"), tc(UNKNOWN, {"z": 1}, "i2")]
    fake = FakeProvider(script, default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_iterations=2, max_tool_errors=2))
    result = agent.run_sync("go")
    assert result.stop_reason == "tool_error"
    assert len(fake.calls) == 2 and result.error == _msg(2, 2)


@pytest.mark.fr("FR-024")
def test_tool_error_on_single_allowed_iteration(kmaker, sink):
    fake = FakeProvider([tc(UNKNOWN, {}, "j1")], default="never")
    agent = _agent(kmaker, fake, limits=RunLimits(max_iterations=1, max_tool_errors=1))
    result = agent.run_sync("go")
    assert result.stop_reason == "tool_error"
    assert len(fake.calls) == 1


# -- FR-025: surfaces --------------------------------------------------------------------------


@pytest.mark.fr("FR-025")
def test_stream_ends_with_run_failed(kmaker, sink):
    fake = _looping_fake()
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=2, max_iterations=10))
    with agent.stream_sync("go") as stream:
        events = list(stream)
        result = stream.result()
    final = events[-1]
    assert str(getattr(final.type, "value", final.type)) == "run.failed"
    assert final.metadata["stop_reason"] == "tool_error"
    assert result.stop_reason == "tool_error" and result.error == _msg(2, 2)
    assert len(fake.calls) == 2
    done = ev(sink, "run.completed")
    assert len(done) == 1 and done[0]["stop_reason"] == "tool_error"


@pytest.mark.fr("FR-025", "FR-023")
def test_async_run(kmaker, sink):
    fake = _looping_fake()
    agent = _agent(kmaker, fake, limits=RunLimits(max_tool_errors=1, max_iterations=10))

    async def go():
        return await agent.run("go")

    result = asyncio.run(asyncio.wait_for(go(), 30))
    assert result.stop_reason == "tool_error" and result.error == _msg(1, 1)
    assert result.iterations == 1 and len(fake.calls) == 1
