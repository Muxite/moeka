"""Design: wonhee integration, trusted routing and durable transport boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.ingress import IngressReceipts
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig
from nanobot.config.schema import CreativeBridgeConfig, PeerConfig
from nanobot.creative_bridge import CreativeBridge
from nanobot.peer_context import validate_peer_context
from nanobot.peers import PeerService, _sign


@pytest.fixture
def context():
    return {
        "version": 1, "project_id": 7, "request_id": "request-1",
        "route": {"channel": "telegram", "chat_id": "123", "owner_id": "owner",
                  "message_id": 9, "message_thread_id": 4, "route_key": "telegram:123:4:owner"},
        "assets": [{"id": 2, "ref": "a" * 64, "role": "raw"}],
    }


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr("nanobot.creative_bridge.get_media_dir", lambda _: media)
    peers = SimpleNamespace(peers={"peer-1": object()}, enqueue=AsyncMock(), result_sink=None)
    config = CreativeBridgeConfig(enabled=True, peer_id="peer-1", python="/usr/bin/python3",
                                  cwd=str(tmp_path), home=str(tmp_path / "data"))
    return CreativeBridge(config, workspace=tmp_path, bus=MessageBus(), peers=peers,
                          get_channel=lambda _: None)


def test_peer_context_rejects_arbitrary_paths_and_untrusted_destinations(context):
    assert validate_peer_context(context)["project_id"] == 7
    context["assets"][0]["ref"] = "/home/owner/secrets"
    with pytest.raises(ValidationError):
        validate_peer_context(context)
    context["assets"] = []
    context["route"]["channel"] = "email"
    with pytest.raises(ValidationError):
        validate_peer_context(context)


@pytest.mark.asyncio
async def test_context_is_signed_persisted_and_selects_project_session(tmp_path, context):
    peer_id = "a" * 32
    secret = "secret-" * 6
    bus = MessageBus()
    service = PeerService(instance_id="b" * 32,
                          peers=[PeerConfig(id=peer_id, socket_path=str(tmp_path / "a.sock"), secret=secret)],
                          state_dir=tmp_path / "state", socket_path=tmp_path / "b.sock", bus=bus)
    envelope = {"id": "request-1", "sender": peer_id, "recipient": "b" * 32,
                "content": "make a reel", "reply_to": None, "hops": 1, "context": context}
    assert await service._handle_envelope(envelope, _sign(secret, envelope)) == {"ack": "request-1"}
    msg = await bus.consume_inbound()
    assert msg.session_key == f"peer:{peer_id}:creative:7"
    assert msg.metadata["creative"] == context
    assert msg.metadata["creative"]["route"]["message_thread_id"] == 4
    envelope["context"]["project_id"] = 8
    with pytest.raises(ValueError, match="authentication"):
        await service._handle_envelope(envelope, "invalid-signature")


@pytest.mark.asyncio
async def test_handoff_receipt_and_forward_are_idempotent(bridge):
    calls = []

    async def call(action, **data):
        calls.append((action, data))
        if action == "inbox_add":
            return {"id": 3, "ref": "a" * 64}
        return {"id": 7, "name": "campus"}

    bridge.call = call
    msg = InboundMessage(channel="telegram", chat_id="123", sender_id="owner", content="use this take",
                         metadata={"message_id": 9, "message_thread_id": 4})
    await bridge.handoff(msg, project_name="campus")
    assert await bridge.intercept(msg)
    assert await bridge.intercept(msg)
    with bridge._db() as db:
        rows = list(db.execute("SELECT * FROM receipt"))
    assert len(rows) == 1
    await bridge._forward(rows[0])
    request = bridge.peers.enqueue.await_args.kwargs
    assert request["context"]["project_id"] == 7
    assert request["context"]["route"]["message_thread_id"] == 4
    assert request["delivery_id"].startswith("creative-")
    await bridge.handoff(msg, leave=True)
    assert not await bridge.intercept(msg)
    assert calls[-1][1]["project_id"] is None


@pytest.mark.asyncio
async def test_attachment_escape_is_refused_and_not_forwarded(bridge, tmp_path):
    source = tmp_path / "secret.txt"
    source.write_text("private")
    bridge.call = AsyncMock(return_value={"id": 7, "name": "campus"})
    msg = InboundMessage(channel="telegram", chat_id="123", sender_id="owner", content="use this",
                         media=[str(source)], metadata={"message_id": 9})
    await bridge.handoff(msg)
    await bridge.intercept(msg)
    with bridge._db() as db:
        row = db.execute("SELECT * FROM receipt").fetchone()
    with pytest.raises(ValueError, match="downloaded Telegram"):
        await bridge._forward(row)
    bridge.peers.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_peer_completion_is_durably_queued_before_transport_ack(bridge, context):
    message = OutboundMessage(channel="telegram", chat_id="123", content="ready", metadata={
        "creative": context, "peer_result": {"result_id": "result-1"},
    })
    await bridge.queue_peer_result(message)
    await bridge.queue_peer_result(message)
    bridge._send = AsyncMock(return_value=[51])
    with bridge._db() as db:
        assert db.execute("SELECT state FROM outbound").fetchone()[0] == "queued"
        assert db.execute("SELECT count(*) FROM outbound").fetchone()[0] == 1
    await bridge._poll_outbound()
    with bridge._db() as db:
        row = db.execute("SELECT state,message_ids FROM outbound").fetchone()
        assert tuple(row) == ("sent", "[51]")
        assert db.execute("SELECT project_id FROM delivered WHERE chat_id='123' AND message_id=51").fetchone()[0] == 7


@pytest.mark.asyncio
async def test_ambiguous_send_remains_uncertain_without_automatic_resend(bridge, context):
    await bridge.queue_text("out-1", "ready", context["route"], 7)
    bridge._send = AsyncMock(side_effect=TimeoutError())
    await bridge._poll_outbound()
    await bridge._poll_outbound()
    assert bridge._send.await_count == 1
    with bridge._db() as db:
        assert db.execute("SELECT state FROM outbound").fetchone()[0] == "uncertain"


@pytest.mark.asyncio
async def test_registered_telegram_send_streams_and_returns_actual_ids(tmp_path):
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]), MessageBus())
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    handles = []

    async def send_video(**kwargs):
        stream = kwargs["video"].input_file_content
        assert stream.read() == b"fake-video"
        assert kwargs["message_thread_id"] == 4
        handles.append(stream)
        return SimpleNamespace(message_id=11)

    bot = SimpleNamespace(send_video=send_video, send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
    channel._wait_for_app = AsyncMock(return_value=SimpleNamespace(bot=bot))
    ids = await channel.send_registered(OutboundMessage(channel="telegram", chat_id="123", content="done",
                                                       media=[str(video)], metadata={"message_thread_id": 4}))
    assert ids == [11, 12]
    assert handles[0].closed


def test_telegram_receipts_survive_reopen_until_completed(tmp_path):
    path = tmp_path / "ingress.sqlite3"
    receipts = IngressReceipts(path)
    assert receipts.save(1, "message", '{"update_id":1}')
    reopened = IngressReceipts(path)
    assert reopened.pending() == [(1, "message", '{"update_id":1}')]
    reopened.complete([1])
    assert not reopened.save(1, "message", '{"update_id":1}')
    assert reopened.pending() == []


@pytest.mark.asyncio
async def test_album_waits_for_queued_slow_downloads():
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]), MessageBus())
    channel._handle_message = AsyncMock()
    key = "123:album"
    channel._album_pending[key] = 1
    channel._media_group_buffers[key] = {"sender_id": "owner", "chat_id": "123", "contents": ["first"],
                                         "media": ["a.mp4"], "metadata": {}, "session_key": None}
    task = asyncio.create_task(channel._flush_media_group(key))
    try:
        await asyncio.sleep(0.7)
        channel._handle_message.assert_not_awaited()
        channel._media_group_buffers[key]["media"].append("b.mp4")
        channel._album_pending[key] = 0
        await asyncio.wait_for(task, 1)
        assert channel._handle_message.await_args.kwargs["media"] == ["a.mp4", "b.mp4"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_registered_video_can_be_sent_as_original_document(tmp_path):
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]), MessageBus())
    video = tmp_path / "original.mp4"
    video.write_bytes(b"original bytes")

    async def send_document(**kwargs):
        assert kwargs["document"].input_file_content.read() == b"original bytes"
        return SimpleNamespace(message_id=15)

    bot = SimpleNamespace(send_document=send_document, send_message=AsyncMock())
    channel._wait_for_app = AsyncMock(return_value=SimpleNamespace(bot=bot))
    ids = await channel.send_registered(OutboundMessage(channel="telegram", chat_id="123", content="",
                                                       media=[str(video)], metadata={"as_document": True}))
    assert ids == [15]


def test_delivery_destination_must_match_real_telegram_route(bridge, context):
    bridge._remember_destination(context["route"])
    bridge._validate_destination(context["route"])
    changed = {**context["route"], "chat_id": "attacker"}
    with pytest.raises(ValueError, match="authenticated"):
        bridge._validate_destination(changed)


@pytest.mark.asyncio
async def test_uncertain_response_has_explicit_status_and_retry_command(bridge, context):
    await bridge.queue_text("out-1", "ready", context["route"], 7)
    with bridge._db() as db:
        db.execute("UPDATE outbound SET state='uncertain' WHERE id='out-1'")
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner", content="/wonhee retry",
                             metadata={"message_id": 20, "message_thread_id": 4})
    assert await bridge.intercept(message)
    with bridge._db() as db:
        assert db.execute("SELECT state FROM outbound WHERE id='out-1'").fetchone()[0] == "queued"


@pytest.mark.asyncio
async def test_active_route_refreshes_canonical_project_and_natural_leave(bridge):
    bridge.call = AsyncMock(return_value={"id": 7, "name": "campus"})
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner", content="use this take",
                             metadata={"message_id": 1, "message_thread_id": 4})
    await bridge.handoff(message, project_name="campus")
    bridge.call = AsyncMock(return_value={"id": 8, "name": "night"})
    assert await bridge.intercept(message)
    bridge.call.assert_awaited_once_with("project_show", route_key="telegram:123:4:owner")
    with bridge._db() as db:
        assert db.execute("SELECT project_id FROM route").fetchone()[0] == 8
        assert db.execute("SELECT project_id FROM receipt").fetchone()[0] == 8
    bridge.call = AsyncMock(return_value={"id": None})
    message.metadata["message_id"] = 2
    assert not await bridge.intercept(message)
    with bridge._db() as db:
        assert db.execute("SELECT count(*) FROM route").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM receipt").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reply_project_precedes_refreshed_selection(bridge):
    bridge.call = AsyncMock(return_value={"id": 7, "name": "campus"})
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner", content="revise this",
                             metadata={"message_id": 2, "message_thread_id": 4, "reply_to_message_id": 15})
    await bridge.handoff(message, project_name="campus")
    with bridge._db() as db:
        db.execute("INSERT INTO delivered VALUES('123',15,6)")
    bridge.call = AsyncMock(return_value={"id": 8, "name": "night"})
    assert await bridge.intercept(message)
    with bridge._db() as db:
        assert db.execute("SELECT project_id FROM route").fetchone()[0] == 8
        assert db.execute("SELECT project_id FROM receipt").fetchone()[0] == 6


def test_bridge_runtime_configuration_accepts_documented_json_name(tmp_path):
    """The installed camelCase config must survive the actual root Config boundary."""
    from nanobot.config.schema import Config

    config = Config.model_validate({"creativeBridge": {
        "enabled": True, "peerId": "a" * 32, "python": "/usr/bin/python3",
        "cwd": str(tmp_path), "home": str(tmp_path / "data"),
    }})
    assert config.creative_bridge.enabled
    assert config.creative_bridge.peer_id == "a" * 32


@pytest.mark.asyncio
async def test_failed_submission_notice_is_durable_and_idempotent(bridge, context):
    """Render failure is reported once even when every status poll repeats it."""
    route = context["route"]
    bridge.call = AsyncMock(return_value={
        "deliveries": [], "failures": [{"id": 42, "project_id": 7, "route": route}],
    })
    await bridge._poll_deliveries()
    await bridge._poll_deliveries()
    with bridge._db() as db:
        rows = list(db.execute("SELECT id,payload,state FROM outbound"))
    assert len(rows) == 1
    assert rows[0]["id"] == "submission-failed:42"
    assert rows[0]["state"] == "queued"
    assert "original take and request are saved" in rows[0]["payload"]
    bridge._send = AsyncMock(return_value=[77])
    await bridge._poll_outbound()
    await bridge._poll_deliveries()
    await bridge._poll_outbound()
    bridge._send.assert_awaited_once()
    with bridge._db() as db:
        assert db.execute("SELECT state FROM outbound WHERE id='submission-failed:42'").fetchone()[0] == "sent"
        assert db.execute("SELECT project_id FROM delivered WHERE chat_id='123' AND message_id=77").fetchone()[0] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [(ValueError("invalid staging path"), "failed"),
                                              (TimeoutError("ambiguous transport"), "uncertain")])
async def test_delivery_failure_state_distinguishes_preflight_and_transport(bridge, context, failure, expected):
    """Definite preflight rejection differs from a possibly accepted network send."""
    ready = asyncio.Event()
    ready.set()
    bridge.get_channel = lambda _: SimpleNamespace(_app_ready=ready)
    calls = []

    async def call(action, **data):
        calls.append((action, data))
        if not data:
            return {"deliveries": [{"id": 12, "project_id": 7, "route": context["route"]}]}
        return {}

    bridge.call = call
    bridge._send = AsyncMock(side_effect=failure)
    await bridge._poll_deliveries()
    assert calls == [
        ("delivery_show", {}),
        ("delivery_show", {"delivery_id": 12, "transition": "sending"}),
        ("delivery_show", {"delivery_id": 12, "transition": expected, "error": type(failure).__name__}),
    ]
