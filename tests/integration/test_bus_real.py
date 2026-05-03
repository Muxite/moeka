"""Integration tests for the MessageBus — real async event flow, no mocks.

Tests publish/consume inbound and outbound messages under real asyncio
concurrency without patching any bus internals.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# inbound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_and_consume_inbound() -> None:
    bus = MessageBus()
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="hello")
    await bus.publish_inbound(msg)
    received = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
    assert received.content == "hello"
    assert received.channel == "telegram"


@pytest.mark.asyncio
async def test_multiple_inbound_messages_are_fifo() -> None:
    bus = MessageBus()
    for i in range(5):
        await bus.publish_inbound(
            InboundMessage(channel="test", sender_id="u", chat_id="c", content=f"msg{i}")
        )
    for i in range(5):
        received = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
        assert received.content == f"msg{i}"


@pytest.mark.asyncio
async def test_inbound_metadata_is_preserved() -> None:
    bus = MessageBus()
    meta = {"key": "value", "_stream": True}
    await bus.publish_inbound(
        InboundMessage(channel="x", sender_id="s", chat_id="c", content="t", metadata=meta)
    )
    received = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
    assert received.metadata["key"] == "value"
    assert received.metadata["_stream"] is True


@pytest.mark.asyncio
async def test_inbound_consume_blocks_until_published() -> None:
    bus = MessageBus()

    async def _publish_after_delay():
        await asyncio.sleep(0.05)
        await bus.publish_inbound(
            InboundMessage(channel="x", sender_id="s", chat_id="c", content="delayed")
        )

    asyncio.create_task(_publish_after_delay())
    received = await asyncio.wait_for(bus.consume_inbound(), timeout=2.0)
    assert received.content == "delayed"


# ---------------------------------------------------------------------------
# outbound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_and_consume_outbound() -> None:
    bus = MessageBus()
    msg = OutboundMessage(channel="telegram", chat_id="c1", content="reply")
    await bus.publish_outbound(msg)
    received = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert received.content == "reply"
    assert received.channel == "telegram"


@pytest.mark.asyncio
async def test_multiple_outbound_messages_are_fifo() -> None:
    bus = MessageBus()
    for i in range(5):
        await bus.publish_outbound(
            OutboundMessage(channel="x", chat_id="c", content=f"out{i}")
        )
    for i in range(5):
        received = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert received.content == f"out{i}"


@pytest.mark.asyncio
async def test_outbound_with_media_list() -> None:
    bus = MessageBus()
    await bus.publish_outbound(
        OutboundMessage(channel="x", chat_id="c", content="txt", media=["/tmp/a.jpg"])
    )
    received = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert received.media == ["/tmp/a.jpg"]


# ---------------------------------------------------------------------------
# inbound / outbound are independent queues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_and_outbound_queues_independent() -> None:
    bus = MessageBus()
    await bus.publish_inbound(
        InboundMessage(channel="x", sender_id="s", chat_id="c", content="in")
    )
    await bus.publish_outbound(OutboundMessage(channel="x", chat_id="c", content="out"))

    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

    assert inbound.content == "in"
    assert outbound.content == "out"


@pytest.mark.asyncio
async def test_high_throughput_inbound(n: int = 500) -> None:
    """Publish and consume 500 messages without losing any."""
    bus = MessageBus()
    for i in range(n):
        await bus.publish_inbound(
            InboundMessage(channel="x", sender_id="s", chat_id="c", content=str(i))
        )
    received = []
    for _ in range(n):
        msg = await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)
        received.append(int(msg.content))
    assert received == list(range(n))


@pytest.mark.asyncio
async def test_concurrent_producers_and_consumer() -> None:
    """Multiple concurrent producers, one consumer — all messages arrive."""
    bus = MessageBus()
    n_producers = 10
    msgs_per_producer = 20
    total = n_producers * msgs_per_producer

    async def _produce(producer_id: int) -> None:
        for i in range(msgs_per_producer):
            await bus.publish_inbound(
                InboundMessage(
                    channel="x", sender_id=str(producer_id),
                    chat_id="c", content=f"{producer_id}:{i}",
                )
            )

    await asyncio.gather(*[_produce(p) for p in range(n_producers)])

    received = set()
    for _ in range(total):
        msg = await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)
        received.add(msg.content)
    assert len(received) == total
