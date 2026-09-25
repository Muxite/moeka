"""Dream runs by direct invocation; no scheduler is involved."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.providers.base import LLMResponse


@pytest.fixture
def dream_loop(tmp_path):
    """A real AgentLoop over a workspace with pending Dream history."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    store = MemoryStore(tmp_path)
    store.write_soul("# Soul")
    store.write_memory("# Memory")
    store.append_history("user prefers tabs")
    store.append_history("project X uses postgres")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.supports_tools = True
    provider.generation = MagicMock(max_tokens=4096)
    calls: list[dict] = []

    async def chat_stream_with_retry(**kwargs):
        calls.append(kwargs)
        return LLMResponse(content="done", finish_reason="stop")

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=32_000,
    )
    return loop, store, calls


async def test_dream_primitives_run_directly_without_a_scheduler(dream_loop):
    """The public Dream primitives compose into a full run with no cron involved."""
    loop, store, calls = dream_loop
    assert store.get_last_dream_cursor() == 0

    result = store.build_dream_prompt()
    assert result is not None
    prompt, last_cursor = result
    resp = await loop.process_direct(
        prompt,
        session_key=MemoryStore.dream_session_key(),
        ephemeral=True,
        tools=store.build_dream_tools(),
        runtime=loop.dream_runtime(),
    )
    assert MemoryStore.dream_run_completed(resp)
    store.set_last_dream_cursor(last_cursor)

    assert calls, "Dream must reach the provider"
    assert store.get_last_dream_cursor() == last_cursor == store.get_latest_cursor()
    # History consumed: nothing left for the next Dream batch.
    assert store.build_dream_prompt() is None


async def test_loop_run_dream_consumes_history_and_advances_cursor(dream_loop):
    loop, store, calls = dream_loop
    latest = store.get_latest_cursor()

    result = await loop.run_dream()

    assert result.status == "completed"
    assert result.cursor == latest
    assert calls, "Dream must reach the provider"
    assert store.get_last_dream_cursor() == latest
    assert store.build_dream_prompt() is None


async def test_loop_run_dream_reports_no_input_without_calling_provider(dream_loop):
    loop, store, calls = dream_loop
    store.set_last_dream_cursor(store.get_latest_cursor())

    result = await loop.run_dream()

    assert result.status == "no_input"
    assert calls == []
