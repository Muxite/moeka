"""Agents on a kernel (Task 9A): specs, runs, stop reasons, budget, trace, lifecycle."""

from __future__ import annotations

import asyncio
import gc
import threading
import warnings
from pathlib import Path
from typing import Any

import pytest

from moeka.agents import Agent, AgentSpec, AskUser, RunLimits, RunResult, ToolInfo
from moeka.budget import CapBudget
from moeka.errors import AuthError, BudgetExceeded
from moeka.testing import FakeProvider, error
from moeka.trace import MemoryTraceSink
from moeka.variants import Variant
from nanobot.config.schema import AgentProfileConfig, InlineSkillConfig
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.policy import DefaultPolicy, IntersectionPolicy, policy_deny
from nanobot.kernel.sampling import Sampling
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)


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


def tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=10, output_tokens=5),
    )


LIST_DIR = tool_call("list_dir", {"path": "."})


@pytest.fixture
def sink() -> MemoryTraceSink:
    return MemoryTraceSink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(fake: FakeProvider | None = None, **kwargs: Any) -> Kernel:
        kernel = Kernel(_env(tmp_path, sink), **kwargs)
        if fake is not None:
            kernel.llm.register_provider("main", fake, MAIN)
        kernels.append(kernel)
        return kernel

    yield make
    for kernel in kernels:
        kernel.close()


def _for_trace(sink: MemoryTraceSink, trace_id: str | None) -> list[dict[str, Any]]:
    return [e for e in sink.events if e.get("trace_id") == trace_id]


# -- specs ---------------------------------------------------------------------------


def test_spec_is_hashable_and_normalised() -> None:
    a = AgentSpec(name="a", bootstrap={"USER.md": "u"}, tools_deny=["exec"],
                  inline_skills=[{"name": "s", "content": "c"}])
    b = AgentSpec(name="a", bootstrap={"USER.md": "u"}, tools_deny=("exec",),
                  inline_skills=[InlineSkillConfig(name="s", content="c")])
    assert a == b and hash(a) == hash(b)
    assert a.tools_deny == ("exec",)
    assert a.inline_skills[0]["name"] == "s"
    with pytest.raises(ValueError):
        AgentSpec(name="")
    with pytest.raises(ValueError):
        RunLimits(max_iterations=0)
    with pytest.raises(TypeError):
        AgentSpec(name="a", tools_deny="exec")


def test_from_profile_maps_fields(tmp_path: Path) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("from file", encoding="utf-8")
    profile = AgentProfileConfig(
        model_preset="main",
        system_prompt_file=str(persona),
        tools_allow=["read_file"],
        tools_deny=["exec"],
        skills_include=["memory"],
        skills_exclude=["github"],
        skills_inline=[InlineSkillConfig(name="s", content="c")],
        memory_enabled=False,
        vec_collections=["docs"],
    )
    spec = AgentSpec.from_profile(profile, name="research")
    assert spec.name == "research"
    assert spec.model == "main"
    assert spec.system_prompt == "from file"
    assert spec.tools_allow == ("read_file",)
    assert spec.tools_deny == ("exec",)
    assert spec.skills_include == ("memory",)
    assert spec.skills_exclude == ("github",)
    assert spec.inline_skills[0]["name"] == "s"
    assert spec.memory is False
    assert spec.doc_scopes == ("docs",)
    inline = AgentSpec.from_profile(AgentProfileConfig(system_prompt="inline"))
    assert inline.name == "default" and inline.system_prompt == "inline"
    assert inline.memory is True and inline.tools_allow is None
    with pytest.raises(ValueError, match="planning"):
        AgentSpec.from_profile(AgentProfileConfig(planning=True))


def test_part_b_fields_raise(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    for spec in (
        AgentSpec(name="x", actions=[lambda: None]),
        AgentSpec(name="x", mcp_servers={"s": {}}),
        AgentSpec(name="x", offline=True),
        AgentSpec(name="x", limits=RunLimits(max_tool_errors=3)),
    ):
        with pytest.raises(NotImplementedError):
            kernel.agent(spec)


def test_kernel_agent_caches_per_spec(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    a = kernel.agent(AgentSpec(name="a"))
    assert kernel.agent(AgentSpec(name="a")) is a
    assert kernel.agent(AgentSpec(name="b")) is not a
    assert isinstance(a, Agent)
    with pytest.raises(TypeError):
        kernel.agent("a")  # type: ignore[arg-type]


def test_spec_shapes_the_loop(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    agent = kernel.agent(AgentSpec(
        name="scoped", system_prompt="You are SCOPED.", tools_allow=("read_file", "list_dir"),
        bootstrap={"USER.md": "user notes"},
    ))
    tools = agent.tools
    assert {t.name for t in tools} == {"read_file", "list_dir"}
    assert all(isinstance(t, ToolInfo) for t in tools)
    assert next(t for t in tools if t.name == "read_file").read_only is True
    loop = agent._loop
    assert loop is not None
    prompt = loop.context.build_system_prompt(include_memory=False, trace=False)
    assert "You are SCOPED." in prompt and "user notes" in prompt
    assert loop.sessions is kernel._session_manager()
    assert loop.env is kernel.core_env
    assert loop.workspace == kernel.env.paths.work_dir


def test_policy_is_intersection_of_kernel_and_spec(make_kernel) -> None:
    kp, sp = DefaultPolicy(), DefaultPolicy(deny_capabilities=frozenset({"web.fetch"}))
    kernel = make_kernel(FakeProvider(), policy=kp)
    assert kernel.policy is kp
    loop_policy = kernel.agent(AgentSpec(name="p", policy=sp))
    loop_policy.tools  # noqa: B018 - builds the loop
    assert isinstance(loop_policy._loop.policy, IntersectionPolicy)
    assert loop_policy._loop.policy.members == (kp, sp)
    only_kernel = kernel.agent(AgentSpec(name="k"))
    only_kernel.tools  # noqa: B018
    assert only_kernel._loop.policy is kp


# -- runs and stop reasons -------------------------------------------------------------


async def test_completed_run(make_kernel, sink) -> None:
    fake = FakeProvider(["hello there"])
    kernel = make_kernel(fake)
    result = await kernel.agent(AgentSpec(name="a")).run("hi")
    assert isinstance(result, RunResult)
    assert result.content == "hello there"
    assert result.stop_reason == "completed"
    assert result.iterations == 1
    assert result.usage.input_tokens > 0
    assert result.cost_usd is not None and result.cost_usd > 0
    assert result.session_key == "agent:a"
    assert result.trace_id is not None
    assert result.error is None and result.question is None
    assert result.messages[-1]["content"] == "hello there"


async def test_tool_run_trace_events_share_trace_id(make_kernel, sink) -> None:
    fake = FakeProvider([LIST_DIR, "done"])
    kernel = make_kernel(fake)
    result = await kernel.agent(AgentSpec(name="a")).run("list", tags={"tenant": "t1"})
    assert result.stop_reason == "completed"
    assert result.tools_used == ("list_dir",)
    assert result.iterations == 2
    events = _for_trace(sink, result.trace_id)
    names = [e["event"] for e in events]
    for name in ("run.started", "iteration", "tool.call", "run.completed", "model.call"):
        assert name in names, name
    assert names.count("model.call") == 2
    assert all(e["tags"].get("agent") == "a" for e in events)
    assert all(e["tags"].get("tenant") == "t1" for e in events)
    assert all(e["span"] == "agent.run" for e in events)
    assert result.cost_usd == pytest.approx(
        sum(e["cost_usd"] for e in events if e["event"] == "model.call")
    )


async def test_max_iterations(make_kernel) -> None:
    fake = FakeProvider(default=LIST_DIR)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="m", limits=RunLimits(max_iterations=2)))
    result = await agent.run("loop forever")
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 2


class _DenyAll(DefaultPolicy):
    def decide(self, principal, req, ctx):
        return policy_deny(principal, req, "test denies everything")


async def test_policy_denials(make_kernel) -> None:
    fake = FakeProvider(default=LIST_DIR)
    kernel = make_kernel(fake)
    spec = AgentSpec(name="d", policy=_DenyAll(), limits=RunLimits(max_policy_denials=2))
    result = await kernel.agent(spec).run("list")
    assert result.stop_reason == "policy_denials"
    assert result.iterations == 2


async def test_ask_user_keeps_options(make_kernel) -> None:
    fake = FakeProvider([tool_call("ask_user", {"question": "Which one?", "options": ["a", "b"]})])
    kernel = make_kernel(fake)
    result = await kernel.agent(AgentSpec(name="q")).run("help")
    assert result.stop_reason == "ask_user"
    assert result.question == AskUser(question="Which one?", options=("a", "b"))


async def test_provider_error_is_typed(make_kernel) -> None:
    fake = FakeProvider([error(401, "bad key", kind="authentication_error")])
    kernel = make_kernel(fake)
    result = await kernel.agent(AgentSpec(name="e")).run("hi")
    assert result.stop_reason == "error"
    assert isinstance(result.error, AuthError)
    assert result.error.model == "fake-main"


async def test_empty_final_response(make_kernel) -> None:
    kernel = make_kernel(FakeProvider(default=""))
    result = await kernel.agent(AgentSpec(name="empty")).run("hi")
    assert result.stop_reason == "empty_final_response"
    assert isinstance(result.error, str)


async def test_deadline(make_kernel, sink) -> None:
    fake = FakeProvider(["late"], delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="slow", limits=RunLimits(deadline_s=30)))
    result = await agent.run("hi", deadline_s=0.2)
    assert result.stop_reason == "deadline"
    assert fake.calls and fake.calls[0].cancelled
    completed = [e for e in _for_trace(sink, result.trace_id) if e["event"] == "run.completed"]
    assert completed and completed[0]["stop_reason"] == "cancelled"


async def test_spec_deadline_applies(make_kernel) -> None:
    kernel = make_kernel(FakeProvider(["late"], delay=5.0))
    agent = kernel.agent(AgentSpec(name="slow", limits=RunLimits(deadline_s=0.2)))
    assert (await agent.run("hi")).stop_reason == "deadline"


class _RefuseAfter:
    """A CapBudget that refuses every admission after the first *n*."""

    def __init__(self, n: int, inner: CapBudget) -> None:
        self.n = n
        self.inner = inner
        self.admits = 0

    def admit(self, estimate):
        self.admits += 1
        if self.admits > self.n:
            raise BudgetExceeded("refused by test", call_id=estimate.call_id)
        return self.inner.admit(estimate)

    def settle(self, reservation, event) -> None:
        self.inner.settle(reservation, event)

    def release(self, reservation) -> None:
        self.inner.release(reservation)


async def test_budget_refusal_mid_run(make_kernel) -> None:
    cap = CapBudget(limit_usd=10.0)
    budget = _RefuseAfter(1, cap)
    fake = FakeProvider([LIST_DIR, "never sent"])
    kernel = make_kernel(fake, budget=budget)
    result = await kernel.agent(AgentSpec(name="b")).run("list")
    assert result.stop_reason == "budget"
    assert isinstance(result.error, BudgetExceeded)
    assert "Error calling" not in result.content
    assert len(fake.calls) == 1
    assert cap.spent_usd > 0


async def test_agent_calls_charged_to_budget_per_tag(make_kernel) -> None:
    cap = CapBudget(limit_usd=10.0, per_tag={"tenant": 5.0})
    kernel = make_kernel(FakeProvider(["ok"]), budget=cap)
    result = await kernel.agent(AgentSpec(name="b")).run("hi", tags={"tenant": "acme"})
    assert result.stop_reason == "completed"
    assert cap.spent_usd > 0
    assert cap.exposure("tenant", "acme") == pytest.approx(cap.spent_usd)
    assert cap.exposure("tenant", "other") == 0.0


async def test_caller_cancel_raises(make_kernel, sink) -> None:
    fake = FakeProvider(["late"], delay=5.0)
    kernel = make_kernel(fake)
    task = asyncio.create_task(kernel.agent(AgentSpec(name="c")).run("hi"))
    while not fake.calls:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if sink.of("run.completed"):
            break
        await asyncio.sleep(0.01)
    assert sink.of("run.completed")[0]["stop_reason"] == "cancelled"


async def test_agent_close_cancels_runs(make_kernel) -> None:
    fake = FakeProvider(["late"], delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="c"))
    task = asyncio.create_task(agent.run("hi"))
    while not fake.calls:
        await asyncio.sleep(0.01)
    await agent.aclose()
    result = await task
    assert result.stop_reason == "cancelled"
    assert result.session_key == "agent:c"
    assert agent.closed
    with pytest.raises(RuntimeError):
        await agent.run("again")
    assert kernel.agent(AgentSpec(name="c")) is not agent


# -- sampling ------------------------------------------------------------------------------


async def test_per_run_sampling_overrides_spec(make_kernel) -> None:
    fake = FakeProvider(default="ok")
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="s", sampling=Sampling(temperature=0.5)))
    await agent.run("one")
    await agent.run("two", sampling=Sampling(temperature=0.1, seed=7))
    await agent.run("three")
    temps = [call.kwargs.get("temperature") for call in fake.calls]
    assert temps == [0.5, 0.1, 0.5]
    requests = [call.provider_context.request for call in fake.calls]
    assert (requests[1].sampling.temperature, requests[1].sampling.seed) == (0.1, 7)
    assert [r.sampling.seed for r in (requests[0], requests[2])] == [None, None]


# -- concurrency ------------------------------------------------------------------------------


async def test_two_agents_concurrently_no_crossover(make_kernel, sink) -> None:
    fake = FakeProvider(default="ok", delay=0.05)
    kernel = make_kernel(fake)
    a = kernel.agent(AgentSpec(name="a"))
    b = kernel.agent(AgentSpec(name="b", system_prompt="I am B."))
    ra, rb = await asyncio.gather(a.run("to a"), b.run("to b", session="custom"))
    assert ra.trace_id != rb.trace_id
    assert ra.session_key == "agent:a" and rb.session_key == "custom"
    for result, name, key in ((ra, "a", "agent:a"), (rb, "b", "custom")):
        events = _for_trace(sink, result.trace_id)
        assert [e["event"] for e in events].count("run.started") == 1
        assert all(e["tags"]["agent"] == name for e in events)
        assert all(e["session_key"] == key for e in events if "session_key" in e)
        assert result.cost_usd == pytest.approx(
            sum(e["cost_usd"] for e in events if e["event"] == "model.call")
        )
    assert ra.cost_usd is not None and rb.cost_usd is not None
    assert "I am B." in rb.messages[0]["content"]
    assert "I am B." not in ra.messages[0]["content"]


# -- sync twin ---------------------------------------------------------------------------------


def test_run_sync_from_thread_and_inside_asyncio_run(make_kernel) -> None:
    kernel = make_kernel(FakeProvider(default="sync ok"))
    agent = kernel.agent(AgentSpec(name="s"))
    out: list[RunResult] = []
    thread = threading.Thread(target=lambda: out.append(agent.run_sync("hi")))
    thread.start()
    thread.join(30)
    assert out and out[0].content == "sync ok"

    async def main() -> RunResult:
        return agent.run_sync("again")

    assert asyncio.run(main()).stop_reason == "completed"


# -- variant and fingerprint --------------------------------------------------------------------


def test_variant_reaches_agent(make_kernel) -> None:
    variant = Variant(name="v1", tool_descriptions={"list_dir": "VARIANT LIST DIR"})
    base = make_kernel(FakeProvider()).agent(AgentSpec(name="a"))
    varied = make_kernel(FakeProvider(), variant=variant).agent(AgentSpec(name="a"))
    by_name = {t.name: t for t in varied.tools}
    assert by_name["list_dir"].description == "VARIANT LIST DIR"
    assert {t.name: t for t in base.tools}["list_dir"].description != "VARIANT LIST DIR"
    fb, fv = base.fingerprint(), varied.fingerprint()
    assert fb.components["tools"] != fv.components["tools"]
    assert fb.components["model"] == fv.components["model"]
    assert base.fingerprint() == fb


def test_fingerprint_tracks_spec_sampling(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    plain = kernel.agent(AgentSpec(name="a")).fingerprint()
    hot = kernel.agent(AgentSpec(name="a", sampling=Sampling(temperature=1.0))).fingerprint()
    assert plain.components["sampling"] != hot.components["sampling"]
    assert plain.components["system_prompt"] == hot.components["system_prompt"]


# -- lifecycle -----------------------------------------------------------------------------------


async def test_kernel_aclose_closes_agents_and_sessions(tmp_path, sink) -> None:
    kernel = Kernel(_env(tmp_path, sink))
    kernel.llm.register_provider("main", FakeProvider(default="ok"), MAIN)
    agent = kernel.agent(AgentSpec(name="a"))
    await agent.run("hi")
    sessions = kernel._session_manager()
    store = sessions._store
    assert store._conn_obj is not None
    gc.collect()  # earlier tests' garbage must not count against this close
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await kernel.aclose()
        gc.collect()
    assert agent.closed
    assert store._conn_obj is None
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, [str(w.message) for w in leaks]
    with pytest.raises(RuntimeError):
        kernel.agent(AgentSpec(name="a"))


async def test_goal_runtime_template_uses_variant_roots(tmp_path, make_kernel) -> None:
    templates = tmp_path / "templates" / "agent"
    templates.mkdir(parents=True)
    (templates / "goal_runtime.md").write_text("VARIANT GOAL GUIDANCE\n", encoding="utf-8")
    variant = Variant(name="g", templates_dir=tmp_path / "templates")
    kernel = make_kernel(FakeProvider(), variant=variant)
    agent = kernel.agent(AgentSpec(name="g"))
    agent.tools  # noqa: B018 - builds the loop
    tool = agent._loop.tools.get("create_goal")
    assert tool._template_roots == (tmp_path / "templates",)
