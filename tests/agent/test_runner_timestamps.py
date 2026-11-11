"""Runner stamps each message when it is appended, so a saved turn has real timing (spec 015)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


async def test_messages_in_one_run_carry_distinct_real_timestamps():
    provider = MagicMock(spec=LLMProvider)
    calls = {"n": 0}

    async def chat(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
            )
        await asyncio.sleep(0.02)
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_stream_with_retry = chat
    provider.chat_with_retry = chat
    tools = MagicMock()
    tools.get_definitions.return_value = []

    async def slow_tool(*a, **k):
        await asyncio.sleep(0.02)
        return "ok"

    tools.execute = AsyncMock(side_effect=slow_tool)
    result = await AgentRunner().run(
        make_run_spec(
            provider, model="m", tools=tools, max_iterations=3, max_tool_result_chars=1000,
            initial_messages=[{"role": "user", "content": "go"}],
        )
    )
    stamped = [m for m in result.messages if m["role"] in ("assistant", "tool")]
    assert stamped and all(getattr(m, "appended_at", None) for m in stamped)
    assert len({m.appended_at for m in stamped}) == len(stamped)
    assert all("timestamp" not in m for m in stamped)  # provider payload unchanged
