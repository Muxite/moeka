"""Phase-4 thought-process features: reasoning persistence for <think> tags,
tool-failure reflection, and the opt-in planning step."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec, RunnerLimits
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


class _FailingTool(Tool):
    @property
    def name(self):
        return "flaky"

    @property
    def description(self):
        return "always fails"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_):
        raise RuntimeError("simulated failure")


class _ScriptedProvider:
    """Returns scripted LLMResponses; mimics the provider chat surface."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, *, messages, tools=None, model=None, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)

    async def chat_stream_with_retry(self, **kwargs):  # pragma: no cover
        return await self.chat_with_retry(**kwargs)


def _response(content="", tool_calls=(), reasoning_content=None,
              thinking_blocks=None, finish_reason="stop"):
    calls = list(tool_calls)
    return SimpleNamespace(
        content=content,
        tool_calls=calls,
        has_tool_calls=bool(calls),
        should_execute_tools=bool(calls),
        reasoning_content=reasoning_content,
        thinking_blocks=thinking_blocks,
        finish_reason=finish_reason,
        usage=None,
        error_type=None,
    )


def _tool_call(name="flaky", call_id="tc-1"):
    return SimpleNamespace(
        id=call_id,
        name=name,
        arguments={},
        to_openai_tool_call=lambda: {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        },
    )


def _spec(provider_responses, *, limits=None, tools=None):
    registry = tools or ToolRegistry()
    provider = _ScriptedProvider(provider_responses)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "do the thing"}],
        tools=registry,
        model="test/model",
        max_iterations=10,
        max_tool_result_chars=4000,
        limits=limits or RunnerLimits(),
    )
    return AgentRunner(provider), spec, provider


# ---------------------------------------------------------------------------
# 4.1 — <think>-tag reasoning persists like native reasoning
# ---------------------------------------------------------------------------

async def test_inline_think_reasoning_lands_in_message():
    runner, spec, _ = _spec([
        _response("<think>secret chain of thought</think>The answer is 4."),
    ])
    result = await runner.run(spec)
    assert result.final_content == "The answer is 4."
    assistant = [m for m in result.messages if m["role"] == "assistant"][-1]
    assert assistant.get("reasoning_content") == "secret chain of thought"
    assert "<think>" not in assistant["content"]


async def test_native_reasoning_still_persists():
    runner, spec, _ = _spec([
        _response("Answer.", reasoning_content="native reasoning"),
    ])
    result = await runner.run(spec)
    assistant = [m for m in result.messages if m["role"] == "assistant"][-1]
    assert assistant.get("reasoning_content") == "native reasoning"


# ---------------------------------------------------------------------------
# 4.3 — tool-failure reflection
# ---------------------------------------------------------------------------

async def test_reflection_injected_after_threshold_failures():
    registry = ToolRegistry()
    registry.register(_FailingTool())
    responses = [
        _response(tool_calls=[_tool_call(call_id=f"tc-{i}")]) for i in range(3)
    ] + [_response("giving up gracefully")]
    runner, spec, provider = _spec(
        responses,
        tools=registry,
        limits=RunnerLimits(tool_failure_reflection_threshold=3),
    )
    result = await runner.run(spec)
    notes = [
        m for m in result.messages
        if m.get("role") == "user" and "Stop and reassess" in str(m.get("content"))
    ]
    assert len(notes) == 1  # injected exactly once
    assert result.final_content == "giving up gracefully"


async def test_reflection_disabled_at_zero_threshold():
    registry = ToolRegistry()
    registry.register(_FailingTool())
    responses = [
        _response(tool_calls=[_tool_call(call_id=f"tc-{i}")]) for i in range(4)
    ] + [_response("done")]
    runner, spec, _ = _spec(
        responses,
        tools=registry,
        limits=RunnerLimits(tool_failure_reflection_threshold=0),
    )
    result = await runner.run(spec)
    assert not any(
        "Stop and reassess" in str(m.get("content")) for m in result.messages
    )


# ---------------------------------------------------------------------------
# 4.2 — planning step (loop-level)
# ---------------------------------------------------------------------------

@pytest.fixture
def planning_loop(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    provider = _ScriptedProvider([
        _response("1. step one\n2. step two\nSuccess: done"),
    ])
    provider.get_default_model = lambda: "test/model"
    provider.generation = SimpleNamespace(max_tokens=1024)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test/model",
        planning=True,
    )
    return loop, provider


async def test_maybe_plan_appends_planning_note(planning_loop):
    loop, provider = planning_loop
    messages = [{"role": "user", "content": "x" * 120}]
    await loop._maybe_plan(messages)
    assert len(messages) == 2
    assert messages[1]["role"] == "user"
    assert "Planning note" in messages[1]["content"]
    assert "step one" in messages[1]["content"]
    # The planning call saw the user request, not the whole conversation.
    assert provider.calls[0][0]["role"] == "system"


async def test_maybe_plan_skips_trivial_messages(planning_loop):
    loop, provider = planning_loop
    messages = [{"role": "user", "content": "hi"}]
    await loop._maybe_plan(messages)
    assert len(messages) == 1
    assert provider.calls == []


async def test_maybe_plan_survives_provider_failure(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    class _BoomProvider(_ScriptedProvider):
        async def chat_with_retry(self, **kwargs):
            raise RuntimeError("provider down")

    provider = _BoomProvider([])
    provider.get_default_model = lambda: "test/model"
    provider.generation = SimpleNamespace(max_tokens=1024)
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="test/model", planning=True,
    )
    messages = [{"role": "user", "content": "y" * 200}]
    await loop._maybe_plan(messages)  # must not raise
    assert len(messages) == 1
