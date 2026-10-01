"""Trace spans, stamping, the Tracer, sinks, TraceHook and skill events (Task 7)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from moeka.budget import CapBudget
from moeka.errors import BudgetExceeded
from moeka.llm import GenerateOptions, user
from moeka.testing import FakeProvider, reply
from moeka.trace import EVENTS, FanoutSink, JsonlTraceSink, MemoryTraceSink, Tracer
from nanobot.agent.context import ContextBuilder
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.trace import (
    current_span,
    current_tags,
    current_trace_id,
    safe_emit,
    span,
    stamp,
)
from nanobot.kernel.trace_hook import TraceHook
from nanobot.llm_usage.context import current_llm_usage_trace_id
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest
from tests.agent.runner_helpers import make_run_spec

STAMP_KEYS = {"event", "trace_id", "span", "tags", "ts"}

MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def put(self, key: str, completion: Any) -> None:
        self.data[key] = completion


def _env(tmp_path: Path, sink: Any) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
        trace=sink,
    )


@pytest.fixture
def sink() -> MemoryTraceSink:
    return MemoryTraceSink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(**kwargs: Any) -> Kernel:
        kernel = Kernel(_env(tmp_path, sink), **kwargs)
        kernels.append(kernel)
        return kernel

    yield make
    for kernel in kernels:
        kernel.close()


def _fake(kernel: Kernel, *script: Any, **kwargs: Any) -> FakeProvider:
    fake = FakeProvider(script, **kwargs)
    kernel.llm.register_provider(MAIN.name, fake, MAIN)
    return fake


# -- spans and stamping ---------------------------------------------------------------


def test_outside_any_span_events_carry_null_trace(sink) -> None:
    event = {"event": "x", "a": 1}
    safe_emit(sink, event)
    assert event == {"event": "x", "a": 1}  # the caller's dict is not mutated
    [got] = sink.events
    assert got["trace_id"] is None and got["span"] is None and got["tags"] == {}
    assert isinstance(got["ts"], float) and got["a"] == 1


def test_nested_spans_extend_the_path_and_merge_tags(sink) -> None:
    with span("rollout", seed=1, stage="outer") as outer:
        assert current_trace_id() == outer.trace_id and len(outer.trace_id) == 32
        assert current_llm_usage_trace_id() == outer.trace_id
        with span("select", stage="inner", k=3) as inner:
            assert inner.trace_id == outer.trace_id
            assert current_span() == "rollout/select"
            assert current_tags() == {"seed": 1, "stage": "inner", "k": 3}
            safe_emit(sink, {"event": "x"})
            # The event's own keys win; an event tags mapping merges over the span's.
            safe_emit(sink, {"event": "y", "span": "mine", "tags": {"k": 9, "own": True}})
        assert current_span() == "rollout" and current_tags() == {"seed": 1, "stage": "outer"}
    assert current_trace_id() is None and current_span() is None and current_tags() == {}
    assert current_llm_usage_trace_id() is None
    x, y = sink.events
    assert (x["trace_id"], x["span"]) == (outer.trace_id, "rollout/select")
    assert x["tags"] == {"seed": 1, "stage": "inner", "k": 3}
    assert y["span"] == "mine" and y["trace_id"] == outer.trace_id
    assert y["tags"] == {"seed": 1, "stage": "inner", "k": 9, "own": True}


def test_root_spans_mint_distinct_trace_ids() -> None:
    with span("a") as a:
        pass
    with span("a") as b:
        pass
    assert a.trace_id != b.trace_id


def test_span_is_single_use_and_validates_name() -> None:
    s = span("a")
    with s, pytest.raises(RuntimeError, match="already active"):
        s.__enter__()
    with pytest.raises(ValueError):
        span("")


def test_stamping_never_raises() -> None:
    class Weird(dict):
        def setdefault(self, *a: Any) -> Any:
            raise RuntimeError("boom")

    assert stamp({"event": "x", "tags": "not-a-mapping"})["tags"] == "not-a-mapping"
    # A dict subclass whose copy works is still stamped (the copy is a plain dict).
    assert "ts" in stamp(Weird(event="x"))


async def test_concurrent_tasks_keep_their_own_span(sink) -> None:
    """The awork ``stage_scope`` bug: two stages in flight must not swap tags."""
    both_inside = asyncio.Barrier(2)

    async def stage(name: str) -> str:
        async with span("stage", stage=name) as s:
            await both_inside.wait()
            await asyncio.sleep(0)
            safe_emit(sink, {"event": "work", "who": name})
            return s.trace_id  # type: ignore[return-value]

    ids = await asyncio.gather(stage("nli"), stage("extract"))
    assert ids[0] != ids[1]
    by_who = {e["who"]: e for e in sink.of("work")}
    assert by_who["nli"]["tags"] == {"stage": "nli"} and by_who["nli"]["trace_id"] == ids[0]
    assert by_who["extract"]["tags"] == {"stage": "extract"}
    assert by_who["extract"]["trace_id"] == ids[1]


# -- the kernel LLM layer ----------------------------------------------------------------


def test_host_thread_span_survives_the_hop_to_the_kernel_loop(make_kernel, sink) -> None:
    kernel = make_kernel()
    _fake(kernel, "ok")
    with kernel.trace.span("stage", stage="nli") as s:
        assert kernel.llm.generate_sync([user("hi")]).text == "ok"
    [call] = sink.of("model.call")
    assert call["trace_id"] == s.trace_id
    assert call["span"] == "stage"
    assert call["tags"] == {"stage": "nli"}


async def test_async_span_and_opts_tags_override_span_tags(make_kernel, sink) -> None:
    kernel = make_kernel()
    _fake(kernel, "a", "b")
    async with kernel.trace.span("rollout", stage="nli", seed=4) as s:
        await kernel.llm.generate([user("hi")], GenerateOptions(tags={"stage": "judge"}))
    await kernel.llm.generate([user("hi")], GenerateOptions(tags={"stage": "free"}))
    first, second = sink.of("model.call")
    assert first["trace_id"] == s.trace_id and first["span"] == "rollout"
    assert first["tags"] == {"stage": "judge", "seed": 4}
    assert second["trace_id"] is None and second["span"] is None
    assert second["tags"] == {"stage": "free"}


async def test_span_tags_drive_budget_per_tag_caps(make_kernel, sink) -> None:
    budget = CapBudget(per_tag={"stage": 1e-9})
    kernel = make_kernel(budget=budget)
    fake = _fake(kernel, default="ok")
    # Untagged calls are not capped by a per-tag limit.
    assert (await kernel.llm.generate([user("q")])).text == "ok"
    with kernel.trace.span("stage", stage="a"):
        with pytest.raises(BudgetExceeded, match="stage='a'"):
            await kernel.llm.generate([user("q")])
    assert len(fake.calls) == 1
    [refused] = sink.of("budget.refuse")
    assert refused["tags"] == {"stage": "a"} and refused["span"] == "stage"


async def test_cache_hit_carries_span_tags(make_kernel, sink) -> None:
    kernel = make_kernel(cache=DictCache())
    _fake(kernel, "ok")
    await kernel.llm.generate([user("q")])
    with kernel.trace.span("replay", stage="b"):
        assert (await kernel.llm.generate([user("q")])).cached
    [hit] = sink.of("cache.hit")
    assert hit["tags"] == {"stage": "b"} and hit["span"] == "replay"


# -- Tracer.subscribe ---------------------------------------------------------------------


async def test_subscribers_see_kernel_internal_events(make_kernel, sink) -> None:
    kernel = make_kernel(budget=CapBudget())
    _fake(kernel, "ok", "again")
    calls: list[dict[str, Any]] = []
    everything: list[str] = []

    def boom(event: dict[str, Any]) -> None:
        raise RuntimeError("subscriber down")

    unsub_calls = kernel.trace.subscribe("model.call", calls.append)
    kernel.trace.subscribe(None, boom)
    unsub_all = kernel.trace.subscribe(None, lambda e: everything.append(e["event"]))
    await kernel.llm.generate([user("hi")])
    assert len(calls) == 1 and calls[0]["model"] == "fake-main"
    assert everything == ["budget.admit", "model.call"]
    assert [e["event"] for e in sink.events] == ["budget.admit", "model.call"]
    unsub_calls()
    unsub_calls()  # idempotent
    unsub_all()
    await kernel.llm.generate([user("hi")])
    assert len(calls) == 1 and len(everything) == 2
    assert len(sink.of("model.call")) == 2


def test_tracer_isolates_a_failing_host_sink_and_copies_per_subscriber() -> None:
    class Down:
        def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("down")

    tracer = Tracer(Down())
    seen: list[dict[str, Any]] = []

    def mutate(event: dict[str, Any]) -> None:
        event["event"] = "changed"

    tracer.subscribe("x", mutate)
    tracer.subscribe("x", seen.append)
    tracer.subscribe("other", seen.append)
    tracer.emit({"event": "x"})
    assert [e["event"] for e in seen] == ["x"]
    assert STAMP_KEYS <= set(seen[0])
    with pytest.raises(TypeError):
        tracer.subscribe(None, "nope")  # type: ignore[arg-type]


def test_subscribe_rejects_coroutine_functions() -> None:
    tracer = Tracer(MemoryTraceSink())

    async def handler(event: dict) -> None:  # would never be awaited
        pass

    class AsyncCallable:
        async def __call__(self, event: dict) -> None:
            pass

    with pytest.raises(TypeError, match="plain function"):
        tracer.subscribe("model.call", handler)
    with pytest.raises(TypeError, match="plain function"):
        tracer.subscribe(None, AsyncCallable())
    tracer.subscribe(None, lambda event: None)


def test_subscribe_is_thread_safe() -> None:
    tracer = Tracer()
    counts: list[int] = []
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            tracer.subscribe(None, lambda e: None)()

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        tracer.subscribe("x", lambda e: counts.append(1))
        for _ in range(500):
            tracer.emit({"event": "x"})
    finally:
        stop.set()
        worker.join()
    assert len(counts) == 500


# -- sinks ------------------------------------------------------------------------------------


def test_jsonl_sink_round_trip(tmp_path) -> None:
    path = tmp_path / "traces" / "run.jsonl"
    with JsonlTraceSink(path) as jsonl, span("run", seed=1):
        threads = [
            # Threads do not inherit contextvars: hand each a copy of the span context.
            threading.Thread(
                target=contextvars.copy_context().run,
                args=(safe_emit, jsonl, {"event": "e", "i": i, "p": Path("/x")}),
            )
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert jsonl.closed
    jsonl.emit({"event": "late"})  # dropped after close, never raises
    jsonl.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert sorted(r["i"] for r in rows) == list(range(20))
    assert all(r["p"] == "/x" and r["span"] == "run" and r["tags"] == {"seed": 1} for r in rows)
    assert all(STAMP_KEYS <= set(r) for r in rows)


def test_memory_and_fanout_sinks() -> None:
    a, b = MemoryTraceSink(), MemoryTraceSink()

    class Down:
        def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("down")

    fan = FanoutSink(a, Down(), b)
    safe_emit(fan, {"event": "x"})
    safe_emit(fan, {"event": "y"})
    assert [e["event"] for e in a.events] == ["x", "y"] == [e["event"] for e in b.events]
    assert [e["event"] for e in a.of("y")] == ["y"] and len(a) == 2
    a.clear()
    assert a.events == [] and len(b) == 2


# -- TraceHook ------------------------------------------------------------------------------


class _Echo(Tool):
    def __init__(self, name: str, fail: bool = False) -> None:
        self._name = name
        self._fail = fail

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"x": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        if self._fail:
            raise ValueError("bad input")
        return "echoed"


def _usage(n: int) -> LLMUsage:
    return LLMUsage(
        input_tokens=n, output_tokens=1, total_tokens=n + 1, reported_tokens=n + 1,
    )


async def test_trace_hook_event_sequence(sink) -> None:
    tools = ToolRegistry()
    tools.register(_Echo("echo"))
    tools.register(_Echo("broken", fail=True))
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="calling", tool_calls=[
            ToolCallRequest(id="c1", name="echo", arguments={"x": "a"}),
            ToolCallRequest(id="c2", name="broken", arguments={"x": "b"}),
        ], usage=_usage(10)),
        LLMResponse(content="done", tool_calls=[], usage=_usage(20)),
    ])
    hook = TraceHook(sink, session_key="s:1", model="test-model")
    with span("rollout", seed=7) as s:
        await AgentRunner().run(make_run_spec(
            provider, initial_messages=[user("go")], tools=tools, model="test-model",
            max_iterations=4, max_tool_result_chars=10_000, hook=hook,
        ))
    names = [e["event"] for e in sink.events]
    assert names == [
        "run.started", "tool.call", "tool.call", "iteration", "iteration", "run.completed",
    ]
    assert all(e["trace_id"] == s.trace_id and e["tags"] == {"seed": 7} for e in sink.events)
    started = sink.of("run.started")[0]
    assert (started["session_key"], started["model"]) == ("s:1", "test-model")
    ok, bad = sink.of("tool.call")
    assert (ok["tool"], ok["ok"], ok["args_valid"], ok["error_kind"]) == ("echo", True, True, None)
    assert ok["call_id"] == "c1" and ok["duration_ms"] >= 0 and ok["iteration"] == 0
    assert (bad["tool"], bad["ok"], bad["error_kind"]) == ("broken", False, "ValueError")
    assert "bad input" in bad["error"]
    first, second = sink.of("iteration")
    assert (first["iteration"], first["tool_calls"]) == (0, 2)
    assert first["usage"]["input_tokens"] == 10
    assert (second["iteration"], second["tool_calls"]) == (1, 0)
    [done] = sink.of("run.completed")
    assert done["stop_reason"] == "completed" and done["iterations"] == 2
    assert done["usage"]["input_tokens"] == 30 and done["error"] is None
    assert "echo" in done["tools_used"]  # the runner's own tools_used list


class _Strict(_Echo):
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
        }


async def test_invalid_args_give_an_invalid_tool_call_in_the_same_trace(tmp_path, sink) -> None:
    tools = ToolRegistry()
    tools.register(_Strict("count"))
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="calling", tool_calls=[
            ToolCallRequest(id="bad1", name="count", arguments={"n": "three"}),
            ToolCallRequest(id="ghost1", name="nope", arguments={}),
            ToolCallRequest(id="good1", name="count", arguments={"n": 3}),
        ]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    with span("rollout") as s:
        await AgentRunner().run(make_run_spec(
            provider, initial_messages=[user("go")], tools=tools, model="m",
            max_iterations=4, max_tool_result_chars=10_000, hook=TraceHook(sink),
            env=_core_env(tmp_path, sink),
        ))
    calls = {e["call_id"]: e for e in sink.of("tool.call")}
    assert set(calls) == {"bad1", "ghost1", "good1"}
    for bad in ("bad1", "ghost1"):
        event = calls[bad]
        assert (event["ok"], event["args_valid"], event["error_kind"]) == (
            False, False, "invalid_args",
        )
        assert event["duration_ms"] is None and event["trace_id"] == s.trace_id
    assert calls["good1"]["args_valid"] is True and calls["good1"]["ok"] is True
    invalid = {e["call_id"]: e for e in sink.of("tool.invalid")}
    assert set(invalid) == {"bad1", "ghost1"}
    assert all(e["trace_id"] == s.trace_id for e in invalid.values())
    assert invalid["bad1"]["tool"] == "count"


async def test_trace_hook_run_completed_on_exception(sink) -> None:
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=RuntimeError("provider down"))
    hook = TraceHook(sink)
    with pytest.raises(RuntimeError):
        await AgentRunner().run(make_run_spec(
            provider, initial_messages=[user("go")], tools=ToolRegistry(), model="m",
            max_iterations=2, max_tool_result_chars=10_000, hook=hook, session_key="s:2",
        ))
    [done] = sink.of("run.completed")
    assert done["stop_reason"] == "error" and "provider down" in done["error"]
    assert done["session_key"] == "s:2"


# -- args_digest on tool.call (spec 006 K3) ---------------------------------------------------


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"b": 2, "a": 1}, _sha('{"a":1,"b":2}')),
        ('{"b": 2, "a": 1}', _sha('{"a":1,"b":2}')),
        ("not json", _sha('"not json"')),
        (None, _sha("{}")),
        ({"t": "é"}, _sha('{"t":"é"}')),
        ({"n": 1}, _sha('{"n":1}')),
        ({"n": 1.0}, _sha('{"n":1.0}')),
        ([1, "x"], _sha('[1,"x"]')),
    ],
)
def test_args_digest_canonicalises(arguments, expected) -> None:
    from moeka.trace import args_digest

    assert args_digest(arguments) == expected
    assert len(expected) == 64


@pytest.mark.parametrize(
    "arguments", [{"x": float("nan")}, {"x": float("inf")}, {"x": object()}, '{"x": NaN}'],
)
def test_args_digest_is_none_when_not_canonicalisable(arguments) -> None:
    from moeka.trace import args_digest

    assert args_digest(arguments) is None
    circular: dict[str, Any] = {}
    circular["self"] = circular
    assert args_digest(circular) is None


def test_args_digest_distinguishes_int_and_float() -> None:
    from moeka.trace import args_digest

    assert args_digest({"n": 1}) != args_digest({"n": 1.0})


class _Kinds(Tool):
    """One tool per tool.call kind: ok, raise, error result, result_invalid."""

    def __init__(self, name: str, mode: str) -> None:
        self._name = name
        self._mode = mode
        if mode == "schema":
            self.output_schema = {
                "type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"],
            }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "kinds"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    async def execute(self, **kwargs: Any) -> Any:
        from nanobot.agent.tools.base import ToolResult

        if self._mode == "raise":
            raise ValueError("boom")
        if self._mode == "error":
            return ToolResult.error("Error: nope")
        if self._mode == "schema":
            return "not json"
        return "fine"


async def test_args_digest_on_every_tool_call_kind(tmp_path, sink) -> None:
    from moeka.trace import args_digest

    tools = ToolRegistry()
    for mode in ("ok", "raise", "error", "schema"):
        tools.register(_Kinds(mode, mode))
    calls = [
        ToolCallRequest(id="c-ok", name="ok", arguments={"x": "1"}),
        ToolCallRequest(id="c-raise", name="raise", arguments='{"x": "2"}'),
        ToolCallRequest(id="c-error", name="error", arguments={"x": "3"}),
        ToolCallRequest(id="c-schema", name="schema", arguments={"x": "4"}),
        ToolCallRequest(id="c-ghost", name="ghost", arguments={"x": "5"}),
        ToolCallRequest(id="c-bad", name="ok", arguments=None),
        ToolCallRequest(id="c-nan", name="ok", arguments={"x": float("nan")}),
    ]
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="calling", tool_calls=calls),
        LLMResponse(content="done", tool_calls=[]),
    ])
    await AgentRunner().run(make_run_spec(
        provider, initial_messages=[user("go")], tools=tools, model="m",
        max_iterations=4, max_tool_result_chars=10_000, hook=TraceHook(sink),
        env=_core_env(tmp_path, sink),
    ))
    events = {e["call_id"]: e for e in sink.of("tool.call")}
    assert set(events) == {c.id for c in calls}
    kinds = {k: e["error_kind"] for k, e in events.items()}
    assert kinds["c-ok"] is None and kinds["c-raise"] == "ValueError"
    assert kinds["c-error"] == "tool_error" and kinds["c-schema"] == "result_invalid"
    assert kinds["c-ghost"] == "invalid_args" and kinds["c-bad"] == "invalid_args"
    for call in calls:
        event = events[call.id]
        assert "args_digest" in event
        assert event["args_digest"] == args_digest(call.arguments)
        # no raw arguments on the event
        assert "arguments" not in event and "params" not in event
    assert events["c-nan"]["args_digest"] is None
    assert events["c-raise"]["args_digest"] == args_digest({"x": "2"})
    assert set(events["c-ok"]) - STAMP_KEYS == {
        "session_key", "iteration", "tool", "call_id", "ok", "args_valid", "error_kind",
        "error", "duration_ms", "args_digest",
    }


async def test_args_digest_counts_repeats(make_kernel, sink, tmp_path) -> None:
    """SC-003: four calls, two distinct argument values."""
    tools = ToolRegistry()
    tools.register(_Kinds("q", "ok"))
    calls = [
        ToolCallRequest(id="a", name="q", arguments={"x": "x"}),
        ToolCallRequest(id="b", name="q", arguments='{"x": "x"}'),
        ToolCallRequest(id="c", name="q", arguments={"x": "x"}),
        ToolCallRequest(id="d", name="q", arguments={"x": "y"}),
    ]
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="calling", tool_calls=calls),
        LLMResponse(content="done", tool_calls=[]),
    ])
    await AgentRunner().run(make_run_spec(
        provider, initial_messages=[user("go")], tools=tools, model="m",
        max_iterations=4, max_tool_result_chars=10_000, hook=TraceHook(sink),
    ))
    assert len({e["args_digest"] for e in sink.of("tool.call")}) == 2


def test_args_digest_failure_never_raises_out_of_the_hook(sink, monkeypatch) -> None:
    import nanobot.kernel.trace_hook as th

    def boom(_: Any) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(th, "args_digest", boom)
    hook = TraceHook(sink)
    call = ToolCallRequest(id="z", name="t", arguments={})
    asyncio.run(hook.after_execute_tool(
        MagicMock(iteration=0), call, None, {}, "ok",
    ))
    [event] = sink.of("tool.call")
    assert event["args_digest"] is None and event["ok"] is True


def test_events_catalogue_mentions_args_digest() -> None:
    assert "args_digest" in EVENTS["tool.call"]


# -- skills ---------------------------------------------------------------------------------


def _core_env(tmp_path: Path, sink: Any) -> CoreEnvironment:
    class _Empty:
        def section(self, name: str) -> dict[str, Any]:
            return {}

    return CoreEnvironment(
        config=_Empty(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink,
    )


def _skill(work: Path, name: str) -> Path:
    path = work / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: the {name} skill\n---\n\nDo {name}.\n")
    return path


def test_skill_listed_from_the_system_prompt(tmp_path, sink) -> None:
    work = tmp_path / "work"
    _skill(work, "triage")
    builder = ContextBuilder(work, env=_core_env(tmp_path, sink))
    with span("turn"):
        prompt = builder.build_system_prompt(
            include_memory=False, include_memory_recent_history=False,
        )
    [listed] = sink.of("skill.listed")
    assert "triage" in listed["skills"] and "triage" in prompt
    assert listed["span"] == "turn"
    # No env, no event.
    ContextBuilder(work).build_system_prompt(
        include_memory=False, include_memory_recent_history=False,
    )
    assert len(sink.of("skill.listed")) == 1


async def test_skill_read_from_read_file(tmp_path, sink) -> None:
    work = tmp_path / "work"
    skill = _skill(work, "triage")
    (work / "notes.md").write_text("plain")
    ctx = ToolContext(config=ToolsConfig(), workspace=str(work), env=_core_env(tmp_path, sink))
    tool = ReadFileTool.create(ctx)
    assert "Do triage." in str(await tool.execute(path=str(skill)))
    await tool.execute(path=str(work / "notes.md"))
    await tool.execute(path=str(work / "skills" / "missing" / "SKILL.md"))
    [read] = sink.of("skill.read")
    assert read["skill"] == "triage" and read["path"] == str(skill)
    assert read["via"] == "read_file" and read["call_id"] is None


def _calls_response(*calls: ToolCallRequest) -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=list(calls), finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=5, output_tokens=5),
    )


def _skill_agent(make_kernel, tmp_path, *script):
    from moeka.agents import AgentSpec

    kernel = make_kernel()
    _fake(kernel, *script, "done")
    work = kernel.env.paths.work_dir
    work.mkdir(parents=True, exist_ok=True)
    return kernel.agent(AgentSpec(name="sk", tools_allow=("read_file", "grep"))), work


def _by_call(sink) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    reads: dict[str, list[dict[str, Any]]] = {}
    order = [e["event"] for e in sink.events if e["event"] in ("skill.read", "tool.call")]
    for event in sink.of("skill.read"):
        reads.setdefault(event["call_id"], []).append(event)
    return reads, order


async def test_grep_content_emits_one_skill_read_per_skill_file(make_kernel, sink, tmp_path):
    agent, work = _skill_agent(make_kernel, tmp_path, _calls_response(
        ToolCallRequest(id="g1", name="grep", arguments={
            "pattern": "Do ", "path": "skills", "output_mode": "content",
            "context_before": 0, "context_after": 0}),
        ToolCallRequest(id="g2", name="grep", arguments={
            "pattern": "Do ", "path": "skills", "output_mode": "files_with_matches"}),
        ToolCallRequest(id="g3", name="grep", arguments={
            "pattern": "Do ", "path": "skills", "output_mode": "count"}),
    ))
    alpha = _skill(work, "alpha")
    beta = _skill(work, "beta")
    (alpha.parent / "notes.md").write_text("Do not count me\nDo twice\n")
    alpha.write_text(alpha.read_text() + "Do again.\n")  # two matches, one event
    result = await agent.run("go")
    assert result.stop_reason == "completed"
    reads, order = _by_call(sink)
    assert set(reads) == {"g1"}
    assert sorted(e["skill"] for e in reads["g1"]) == ["alpha", "beta"]
    assert {e["path"] for e in reads["g1"]} == {str(alpha.resolve()), str(beta.resolve())}
    assert all(e["via"] == "grep" for e in reads["g1"])
    calls = {e["call_id"]: e for e in sink.of("tool.call")}
    assert set(calls) == {"g1", "g2", "g3"} and calls["g1"]["ok"]
    # every skill.read of the call precedes its tool.call
    events = sink.events
    g1_call = next(i for i, e in enumerate(events)
                   if e["event"] == "tool.call" and e["call_id"] == "g1")
    assert all(i < g1_call for i, e in enumerate(events) if e["event"] == "skill.read")


async def test_read_file_skill_read_carries_call_id(make_kernel, sink, tmp_path):
    agent, work = _skill_agent(make_kernel, tmp_path, _calls_response(
        ToolCallRequest(id="r1", name="read_file", arguments={"path": "skills/alpha/SKILL.md"}),
        ToolCallRequest(id="r2", name="read_file", arguments={
            "path": "skills/alpha/SKILL.md", "offset": 2, "limit": 1, "force": True}),
        ToolCallRequest(id="r3", name="read_file", arguments={"path": "skills/alpha/other.md"}),
        ToolCallRequest(id="r4", name="read_file", arguments={"path": "skills/nope/SKILL.md"}),
        ToolCallRequest(id="r5", name="read_file", arguments={"path": "SKILL.md"}),
        ToolCallRequest(id="r6", name="read_file", arguments={
            "path": "skills/alpha/SKILL.md", "offset": 999}),
    ))
    alpha = _skill(work, "alpha")
    (alpha.parent / "other.md").write_text("other")
    (work / "SKILL.md").write_text("not a skill file")
    await agent.run("go")
    reads, _ = _by_call(sink)
    assert set(reads) == {"r1", "r2"}
    assert all(len(v) == 1 and v[0]["via"] == "read_file" and v[0]["skill"] == "alpha"
               for v in reads.values())
    calls = {e["call_id"]: e for e in sink.of("tool.call")}
    assert not calls["r4"]["ok"] and not calls["r6"]["ok"]


async def test_skill_read_outside_a_runner_has_no_call_id(tmp_path, sink) -> None:
    from nanobot.agent.tools.search import GrepTool

    work = tmp_path / "work"
    _skill(work, "triage")
    ctx = ToolContext(config=ToolsConfig(), workspace=str(work), env=_core_env(tmp_path, sink))
    grep = GrepTool.create(ctx)
    await grep.execute(pattern="Do", path="skills")
    [read] = sink.of("skill.read")
    assert read["call_id"] is None and read["via"] == "grep"
    await grep.execute(pattern="zzz-no-match", path="skills")
    assert len(sink.of("skill.read")) == 1


def test_skill_read_catalogue_mentions_via_and_call_id() -> None:
    assert "via" in EVENTS["skill.read"] and "call_id" in EVENTS["skill.read"]
    assert "lower bound" in EVENTS["skill.read"]


# -- event schema ---------------------------------------------------------------------------


async def test_every_emitted_event_has_the_stamp_and_is_catalogued(
    make_kernel, sink, tmp_path,
) -> None:
    kernel = make_kernel(budget=CapBudget(), cache=DictCache())
    _fake(kernel, reply("ok", input_tokens=3, output_tokens=1))
    async with kernel.trace.span("schema"):
        await kernel.llm.generate([user("q")])
        await kernel.llm.generate([user("q")])  # cache hit
    hook = TraceHook(kernel.trace)
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content="done"))
    await AgentRunner().run(make_run_spec(
        provider, initial_messages=[user("go")], tools=ToolRegistry(), model="m",
        max_iterations=2, max_tool_result_chars=10_000, hook=hook,
    ))
    work = tmp_path / "skills-work"
    skill = _skill(work, "triage")
    env = _core_env(tmp_path, kernel.trace)
    ContextBuilder(work, env=env).build_system_prompt(
        include_memory=False, include_memory_recent_history=False,
    )
    tool = ReadFileTool.create(ToolContext(config=ToolsConfig(), workspace=str(work), env=env))
    await tool.execute(path=str(skill))

    names = {e["event"] for e in sink.events}
    assert {
        "budget.admit", "model.call", "cache.hit", "run.started", "iteration",
        "run.completed", "skill.listed", "skill.read",
    } <= names
    for event in sink.events:
        assert STAMP_KEYS <= set(event), event
        assert isinstance(event["tags"], dict) and isinstance(event["ts"], float)
        assert event["event"] in EVENTS, event["event"]


def test_events_catalogue_covers_the_contract() -> None:
    expected = {
        "policy.decision", "tool.invalid", "tool.result_invalid", "tool.dropped",
        "plugin.load", "plugin.lifecycle", "model.route", "model.call", "fact.recorded",
        "artifact.proposed", "artifact.rejected", "paths.overlap_allowed",
        "sampling.dropped", "budget.admit", "budget.refuse", "cache.hit", "run.started",
        "iteration", "tool.call", "run.completed", "skill.listed", "skill.read",
    }
    assert expected <= set(EVENTS)
    assert all(isinstance(v, str) and v for v in EVENTS.values())
