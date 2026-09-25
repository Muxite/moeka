import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    response = LLMResponse(content="done", tool_calls=[])
    provider.chat_stream_with_retry = AsyncMock(return_value=response)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_process_direct_reuses_existing_session_lock(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    session_key = "api:fixed"
    lock = loop._session_locks.setdefault(session_key, asyncio.Lock())
    await lock.acquire()
    entered = asyncio.Event()

    async def _process_message(msg, **_kwargs):
        entered.set()
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=msg.content)

    loop._process_message = _process_message
    task = asyncio.create_task(loop.process_direct("direct", session_key=session_key))
    try:
        await asyncio.sleep(0)
        assert not entered.is_set()

        lock.release()
        response = await asyncio.wait_for(task, timeout=1.0)

        assert entered.is_set()
        assert response is not None
        assert response.content == "direct"
    finally:
        if lock.locked():
            lock.release()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_process_direct_applies_per_run_hooks(tmp_path) -> None:
    from nanobot.agent.hook import AgentHook, AgentRunHookContext

    loop = _make_loop(tmp_path)
    events: list[tuple[str, str | None]] = []

    class RecordingHook(AgentHook):
        async def before_run(self, context: AgentRunHookContext) -> None:
            events.append(("before", None))

        async def after_run(self, context: AgentRunHookContext) -> None:
            events.append(("after", context.final_content))

    response = await loop.process_direct(
        "hello",
        session_key="api:per-run-hook",
        hooks=[RecordingHook()],
    )

    assert response is not None
    assert response.content == "done"
    assert events == [("before", None), ("after", "done")]


@pytest.mark.asyncio
async def test_process_direct_accepts_media() -> None:
    """process_direct should forward media paths to _process_message."""
    from nanobot.bus.runtime_events import RuntimeEventPublisher

    loop = AgentLoop.__new__(AgentLoop)
    loop._session_locks = {}
    loop.runtime_event_publisher = RuntimeEventPublisher(MessageBus())

    captured_msg = None

    async def fake_process(
        msg,
        *,
        session_key="",
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        ephemeral=False,
    ):
        nonlocal captured_msg
        captured_msg = msg
        return None

    loop._process_message = fake_process

    await loop.process_direct(
        content="analyze this",
        media=["/tmp/image.png", "/tmp/report.pdf"],
        session_key="test:1",
    )

    assert captured_msg is not None
    assert captured_msg.media == ["/tmp/image.png", "/tmp/report.pdf"]
    assert captured_msg.content == "analyze this"
