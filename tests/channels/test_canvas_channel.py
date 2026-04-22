"""Tests for the Canvas LMS channel — focus on bootstrap, dedup, and send routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.canvas import CanvasChannel, CanvasConfig, _mark_seen


def _make_channel(**overrides) -> CanvasChannel:
    """Build a Canvas channel with permissive defaults for tests."""
    cfg = CanvasConfig.model_validate(
        {
            "enabled": True,
            "apiUrl": "https://canvas.example.com",
            "apiToken": "test-token",
            "allowFrom": ["*"],
            **overrides,
        }
    )
    return CanvasChannel(cfg, MessageBus())


class TestMarkSeen:
    def test_returns_true_on_first_insert(self) -> None:
        seen: dict[str, None] = {}
        assert _mark_seen(seen, "a", 10) is True
        assert "a" in seen

    def test_returns_false_on_duplicate(self) -> None:
        seen: dict[str, None] = {"a": None}
        assert _mark_seen(seen, "a", 10) is False

    def test_fifo_eviction_when_over_cap(self) -> None:
        seen: dict[str, None] = {}
        for i in range(5):
            _mark_seen(seen, str(i), max_size=3)
        assert list(seen.keys()) == ["2", "3", "4"]


class TestBootstrapSilence:
    """First poll must not publish historical items to the bus."""

    @pytest.mark.asyncio
    async def test_inbox_bootstrap_marks_seen_without_publishing(self) -> None:
        ch = _make_channel()
        ch._ensure_self_id = AsyncMock(return_value="me")

        convo = {"id": 1, "subject": "hi", "last_message": "msg"}
        full = {
            "messages": [
                {"id": 10, "author_id": "alice", "body": "hello"},
                {"id": 11, "author_id": "alice", "body": "world"},
            ],
            "participants": [{"id": "alice", "name": "Alice"}],
        }
        ch._get_all = AsyncMock(return_value=[convo])
        ch._get = AsyncMock(return_value=full)
        ch._handle_message = AsyncMock()

        await ch._poll_inbox()

        ch._handle_message.assert_not_awaited()
        assert "10" in ch._seen_message_ids
        assert "11" in ch._seen_message_ids
        assert ch._bootstrapped_inbox is True

    @pytest.mark.asyncio
    async def test_second_poll_publishes_only_new_messages(self) -> None:
        ch = _make_channel()
        ch._ensure_self_id = AsyncMock(return_value="me")
        ch._seen_message_ids = {"10": None}
        ch._bootstrapped_inbox = True

        convo = {"id": 1, "subject": "hi", "last_message": "msg"}
        full = {
            "messages": [
                {"id": 10, "author_id": "alice", "body": "already seen"},
                {"id": 11, "author_id": "alice", "body": "brand new"},
            ],
            "participants": [{"id": "alice", "name": "Alice"}],
        }
        ch._get_all = AsyncMock(return_value=[convo])
        ch._get = AsyncMock(return_value=full)
        ch._handle_message = AsyncMock()

        await ch._poll_inbox()

        assert ch._handle_message.await_count == 1
        kwargs = ch._handle_message.await_args.kwargs
        assert "brand new" in kwargs["content"]
        assert kwargs["metadata"]["message_id"] == "11"

    @pytest.mark.asyncio
    async def test_skips_self_authored_messages(self) -> None:
        ch = _make_channel()
        ch._ensure_self_id = AsyncMock(return_value="me")
        ch._bootstrapped_inbox = True

        convo = {"id": 1, "subject": "hi", "last_message": "msg"}
        full = {
            "messages": [{"id": 20, "author_id": "me", "body": "my reply"}],
            "participants": [{"id": "me", "name": "Me"}],
        }
        ch._get_all = AsyncMock(return_value=[convo])
        ch._get = AsyncMock(return_value=full)
        ch._handle_message = AsyncMock()

        await ch._poll_inbox()

        ch._handle_message.assert_not_awaited()
        assert "20" in ch._seen_message_ids


class TestSelfIdCaching:
    @pytest.mark.asyncio
    async def test_self_id_fetched_once(self) -> None:
        ch = _make_channel()
        ch._get = AsyncMock(return_value={"id": 42})

        first = await ch._ensure_self_id()
        second = await ch._ensure_self_id()

        assert first == "42"
        assert second == "42"
        ch._get.assert_awaited_once_with("/api/v1/users/self")


class TestSendRouting:
    @pytest.mark.asyncio
    async def test_inbox_reply_posts_to_canvas(self) -> None:
        ch = _make_channel()
        ch._post = AsyncMock(return_value={})

        msg = OutboundMessage(
            channel="canvas",
            chat_id="77",
            content="hi",
            metadata={"canvas_type": "inbox", "conversation_id": "77"},
        )
        await ch.send(msg)

        ch._post.assert_awaited_once()
        path = ch._post.await_args.args[0]
        assert path == "/api/v1/conversations/77/add_message"
        assert ch._post.await_args.args[1] == {"body": "hi"}

    @pytest.mark.asyncio
    async def test_announcement_message_has_no_reply_target(self) -> None:
        ch = _make_channel()
        ch._post = AsyncMock()

        msg = OutboundMessage(
            channel="canvas",
            chat_id="course:1:announcement:2",
            content="response",
            metadata={"canvas_type": "announcement"},
        )
        await ch.send(msg)

        ch._post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_untyped_course_prefixed_id_does_not_reply(self) -> None:
        """Fallback heuristic: course:… ids never go to /conversations."""
        ch = _make_channel()
        ch._post = AsyncMock()

        msg = OutboundMessage(
            channel="canvas",
            chat_id="course:1:submission:9",
            content="nope",
            metadata={},
        )
        await ch.send(msg)

        ch._post.assert_not_awaited()
