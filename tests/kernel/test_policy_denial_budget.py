"""The per-turn permission-policy denial ceiling (invariant I5, Task 10).

- ``AgentRunSpec.max_policy_denials`` (default 6) bounds ``PermissionPolicy.decide``
  denials per turn; reaching it ends the turn with ``stop_reason == "policy_denials"``.
- Only gate verdicts with ``layer == "policy"`` count. Floor denials (fork bomb,
  protected path) and gate-layer denials (a broken declaration) never do.
- The stop reuses the max-iterations finalization (same prompt, same fallback).
- The counter is per ``run()`` call: a sub-agent's run has its own budget.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.runner import (
    DEFAULT_MAX_POLICY_DENIALS,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
    PolicyDenialBudget,
)
from nanobot.agent.tools.base import Tool, capability_request
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import AgentDefaults
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.gate import GateResult
from nanobot.kernel.policy import (
    POLICY_MARKER,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    policy_deny,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session import turn_continuation
from nanobot.utils.runtime import BUDGET_EXHAUSTED_FINALIZATION_PROMPT
from tests.agent.runner_helpers import make_run_spec

_MAX_CHARS = AgentDefaults().max_tool_result_chars
_FORK_BOMB = ":(){ :|:& };:"
_FINAL = "final answer from the no-tools call"


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


class _NullSink:
    def emit(self, event: dict[str, Any]) -> None:
        pass


def _env(tmp_path: Path) -> CoreEnvironment:
    return CoreEnvironment(
        config=_EmptyConfig(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=_NullSink(),
        strict=True,
    )


class FetchTool(Tool):
    """``net.fetch`` on ``target``; read-only so a concurrent spec batches it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "fetch"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    @property
    def read_only(self) -> bool:
        return True

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request("net.fetch", params.get("target"))]

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "fetched"


class BrokenDeclarationTool(FetchTool):
    @property
    def name(self) -> str:
        return "broken"

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        raise RuntimeError("boom")


class ScriptedModel:
    """Tool-enabled calls follow *plan* (a list of tool-call lists, cycled by a
    factory); no-tools calls are the budget finalization and are recorded."""

    def __init__(self, next_calls, *, finalization: Any = _FINAL) -> None:
        self._next_calls = next_calls
        self._finalization = finalization
        self.tool_rounds = 0
        self.finalization_calls: list[list[dict[str, Any]]] = []

    async def __call__(self, **kwargs: Any) -> LLMResponse:
        if kwargs.get("tools") is None:
            self.finalization_calls.append(list(kwargs["messages"]))
            if isinstance(self._finalization, BaseException):
                raise self._finalization
            if isinstance(self._finalization, LLMResponse):
                return self._finalization
            return LLMResponse(content=self._finalization, tool_calls=[])
        calls = self._next_calls(self.tool_rounds)
        self.tool_rounds += 1
        if not calls:
            return LLMResponse(content="done", tool_calls=[])
        return LLMResponse(content="working", tool_calls=calls)


def _fetch(i: int) -> ToolCallRequest:
    return ToolCallRequest(id=f"f{i}", name="fetch", arguments={"target": f"h{i}.example"})


def _exec(i: int, command: str) -> ToolCallRequest:
    return ToolCallRequest(id=f"e{i}", name="exec", arguments={"command": command})


async def _run(model: ScriptedModel, tools: ToolRegistry, **spec: Any) -> AgentRunResult:
    provider = MagicMock()
    provider.chat_stream_with_retry = model
    spec.setdefault("max_iterations", 200)
    return await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_tool_result_chars=_MAX_CHARS,
        **spec,
    ))


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _policy_denials(result: AgentRunResult) -> int:
    return sum(
        1 for e in result.tool_events
        if e["detail"].startswith(("policy_denial: ", "policy_denial_escalated: "))
    )


# -- spec / gate plumbing -------------------------------------------------------


def test_spec_default_ceiling_is_six():
    assert DEFAULT_MAX_POLICY_DENIALS == 6
    spec = AgentRunSpec(
        initial_messages=[], tools=ToolRegistry(), runtime=MagicMock(),
        max_iterations=1, max_tool_result_chars=1,
    )
    assert spec.max_policy_denials == 6


def test_budget_counts_up_to_the_limit():
    budget = PolicyDenialBudget(limit=2)
    assert not budget.exhausted
    budget.record()
    assert not budget.exhausted
    budget.record()
    assert budget.exhausted and budget.count == 2


def test_policy_capability_only_for_policy_layer():
    """Task 9 deferred minor: a gate-layer (declaration) deny is not a policy deny."""
    deny = Deny(reason=f"Error: x {POLICY_MARKER}", marker=POLICY_MARKER, capability="c")
    assert GateResult(deny, "policy").policy_capability == "c"
    assert GateResult(deny, "gate").policy_capability is None
    assert GateResult(deny, "floor").policy_capability is None


# -- counting -------------------------------------------------------------------


async def test_policy_denials_stop_the_turn_at_the_ceiling():
    model = ScriptedModel(lambda i: [_fetch(i)])
    fetch = FetchTool()
    result = await _run(model, _registry(fetch),
                        policy=DefaultPolicy(deny_rules=(("net.fetch", "*"),)))
    assert result.stop_reason == "policy_denials"
    assert model.tool_rounds == 6 and _policy_denials(result) == 6
    assert fetch.calls == []


async def test_custom_ceiling_is_honoured():
    model = ScriptedModel(lambda i: [_fetch(i)])
    result = await _run(model, _registry(FetchTool()), max_policy_denials=2,
                        policy=DefaultPolicy(deny_rules=(("net.fetch", "*"),)))
    assert result.stop_reason == "policy_denials" and model.tool_rounds == 2


async def test_repeated_floor_denials_do_not_count(tmp_path):
    """Fork bombs hit the exec floor: throttled/escalated as before, never budgeted."""
    model = ScriptedModel(lambda i: [_exec(i, _FORK_BOMB)] if i < 10 else [])
    tools = _registry(ExecTool(working_dir=str(tmp_path)))
    result = await _run(model, tools, max_iterations=20, env=_env(tmp_path))
    assert result.stop_reason == "completed"
    assert model.tool_rounds == 11
    details = [e["detail"] for e in result.tool_events]
    assert len(details) == 10
    assert all(d.startswith("exec_guard_denial") for d in details)
    assert any(d.startswith("exec_guard_denial_escalated") for d in details)
    assert _policy_denials(result) == 0


async def test_protected_path_floor_denials_do_not_count(tmp_path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    env = _env(tmp_path)
    env.paths.state_dir.mkdir(parents=True, exist_ok=True)
    target = str(env.paths.state_dir / "secret")
    model = ScriptedModel(lambda i: [ToolCallRequest(
        id=f"r{i}", name="read_file", arguments={"path": target},
    )] if i < 8 else [])
    tools = _registry(ReadFileTool(workspace=env.paths.work_dir))
    result = await _run(model, tools, max_iterations=20, env=env)
    assert result.stop_reason == "completed"
    assert _policy_denials(result) == 0
    assert all(not e["detail"].startswith("policy") for e in result.tool_events)


async def test_gate_layer_denials_do_not_count():
    model = ScriptedModel(lambda i: [ToolCallRequest(
        id=f"b{i}", name="broken", arguments={"target": "x"},
    )] if i < 8 else [])
    result = await _run(model, _registry(BrokenDeclarationTool()), max_iterations=20)
    assert result.stop_reason == "completed"
    assert len(result.tool_events) == 8
    assert _policy_denials(result) == 0


async def test_mixed_floor_and_policy_denials_count_only_policy(tmp_path):
    def plan(i: int) -> list[ToolCallRequest]:
        # floor, policy, floor, policy, ...
        return [_exec(i, _FORK_BOMB)] if i % 2 == 0 else [_fetch(i)]

    model = ScriptedModel(plan)
    tools = _registry(ExecTool(working_dir=str(tmp_path)), FetchTool())
    result = await _run(model, tools, env=_env(tmp_path),
                        policy=DefaultPolicy(deny_rules=(("net.fetch", "*"),)))
    assert result.stop_reason == "policy_denials"
    assert _policy_denials(result) == 6
    floors = [e for e in result.tool_events if e["detail"].startswith("exec_guard")]
    assert len(floors) == 6
    assert model.tool_rounds == 12


async def test_concurrent_batch_never_overshoots_the_ceiling():
    """Ten denied calls in one concurrent batch: six count, the rest are not run."""
    fetch = FetchTool()
    model = ScriptedModel(lambda i: [_fetch(n) for n in range(10)] if i == 0 else [])
    result = await _run(model, _registry(fetch), concurrent_tools=True,
                        policy=DefaultPolicy(deny_rules=(("net.fetch", "*"),)))
    assert result.stop_reason == "policy_denials"
    assert model.tool_rounds == 1
    assert _policy_denials(result) == 6
    skipped = [e for e in result.tool_events
               if e["detail"].startswith("policy_denial_budget_exhausted")]
    assert len(skipped) == 4
    assert len(result.tool_events) == 10 and fetch.calls == []
    # Every tool call still gets a tool message (the transcript stays well-formed).
    assert sum(1 for m in result.messages if m.get("role") == "tool") == 10


async def test_allowed_calls_after_ceiling_in_same_batch_are_not_run():
    fetch = FetchTool()

    class Policy(DefaultPolicy):
        def decide(self, principal, req, ctx):
            if req.resource.startswith("deny"):
                return policy_deny(principal, req, "test")
            return super().decide(principal, req, ctx)

    calls = [ToolCallRequest(id=f"d{n}", name="fetch", arguments={"target": f"deny{n}"})
             for n in range(6)]
    calls.append(ToolCallRequest(id="ok", name="fetch", arguments={"target": "fine"}))
    model = ScriptedModel(lambda i: calls if i == 0 else [])
    result = await _run(model, _registry(fetch), policy=Policy())
    assert result.stop_reason == "policy_denials"
    assert fetch.calls == []
    assert result.tool_events[-1]["detail"].startswith("policy_denial_budget_exhausted")


# -- finalization parity with max_iterations --------------------------------------


async def _max_iterations_run(**kw: Any) -> tuple[AgentRunResult, ScriptedModel]:
    model = ScriptedModel(lambda i: [_fetch(i)], **kw.pop("model_kw", {}))
    result = await _run(model, _registry(FetchTool()), max_iterations=3, **kw)
    return result, model


async def _denial_run(**kw: Any) -> tuple[AgentRunResult, ScriptedModel]:
    model = ScriptedModel(lambda i: [_fetch(i)], **kw.pop("model_kw", {}))
    result = await _run(model, _registry(FetchTool()), max_iterations=3,
                        max_policy_denials=2,
                        policy=DefaultPolicy(deny_rules=(("net.fetch", "*"),)), **kw)
    return result, model


async def test_finalization_matches_max_iterations():
    max_result, max_model = await _max_iterations_run()
    den_result, den_model = await _denial_run()
    assert max_result.stop_reason == "max_iterations"
    assert den_result.stop_reason == "policy_denials"
    for result, model in ((max_result, max_model), (den_result, den_model)):
        assert len(model.finalization_calls) == 1
        assert model.finalization_calls[0][-1]["content"] == BUDGET_EXHAUSTED_FINALIZATION_PROMPT
        assert result.final_content == _FINAL
        assert result.messages[-1] == {"role": "assistant", "content": _FINAL}


@pytest.mark.parametrize("finalization", [
    RuntimeError("provider down"),
    LLMResponse(content="oops", tool_calls=[], finish_reason="error"),
    LLMResponse(content="   ", tool_calls=[]),
])
async def test_failed_finalization_uses_the_same_fallback(finalization):
    max_result, _ = await _max_iterations_run(model_kw={"finalization": finalization},
                                              max_iterations_message="fallback {max_iterations}")
    den_result, _ = await _denial_run(model_kw={"finalization": finalization},
                                      max_iterations_message="fallback {max_iterations}")
    assert max_result.final_content == den_result.final_content == "fallback 3"


async def test_finalize_disabled_skips_the_call_for_both():
    for runner in (_max_iterations_run, _denial_run):
        result, model = await runner(finalize_on_max_iterations=False,
                                     max_iterations_message="fallback")
        assert model.finalization_calls == []
        assert result.final_content == "fallback"


def test_policy_denial_stop_never_auto_continues_a_goal():
    """Only ``max_iterations`` may hand off to sustained-goal continuation."""
    assert turn_continuation.should_stream_budget_response(
        stop_reason="policy_denials", pending_queue_available=True,
        session_metadata={}, message_metadata={},
    ) is True


# -- independence: each run (parent, sub-agent) has its own budget -----------------


async def test_concurrent_runs_have_independent_budgets():
    """Parent and sub-agent share one AgentRunner and one policy object, not a counter."""
    runner = AgentRunner()
    policy = DefaultPolicy(deny_rules=(("net.fetch", "*"),))

    async def one() -> tuple[AgentRunResult, ScriptedModel]:
        model = ScriptedModel(lambda i: [_fetch(i)])
        provider = MagicMock()
        provider.chat_stream_with_retry = model
        result = await runner.run(make_run_spec(
            provider, initial_messages=[{"role": "user", "content": "go"}],
            tools=_registry(FetchTool()), model="m", max_iterations=200,
            max_tool_result_chars=_MAX_CHARS, policy=policy,
        ))
        return result, model

    (a, am), (b, bm) = await asyncio.gather(one(), one())
    for result, model in ((a, am), (b, bm)):
        assert result.stop_reason == "policy_denials"
        assert model.tool_rounds == 6 and _policy_denials(result) == 6


def _subagent_manager(tmp_path: Path, **kw: Any):
    from nanobot.agent import SubagentManager
    from nanobot.bus.queue import MessageBus

    return SubagentManager(workspace=tmp_path, bus=MessageBus(), max_tool_result_chars=16_000,
                           **kw)


def _subagent_runtime():
    from nanobot.providers.base import GenerationSettings, LLMProvider
    from nanobot.utils.llm_runtime import LLMRuntime

    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=4096)
    return LLMRuntime.capture(provider, "test-model", context_window_tokens=128_000)


@pytest.mark.parametrize("kw, expected", [({}, 6), ({"max_policy_denials": 3}, 3)])
async def test_subagent_spec_carries_its_own_ceiling(tmp_path, kw, expected):
    from nanobot.agent.subagent import SubagentStatus

    sm = _subagent_manager(tmp_path, **kw)
    sm.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="ok", messages=[], stop_reason="completed",
    ))
    with patch.object(sm, "_announce_result", new_callable=AsyncMock):
        await sm._run_subagent(
            "t1", "do task", "label", {"channel": "cli", "chat_id": "direct"},
            SubagentStatus(task_id="t1", label="label", task_description="do task",
                           started_at=time.monotonic()),
            _subagent_runtime(),
        )
    spec = sm.runner.run.await_args.args[0]
    assert spec.max_policy_denials == expected
