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
from nanobot.creative_bridge import CreativeActionError, CreativeBridge
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


@pytest.mark.asyncio
@pytest.mark.parametrize("code,retryable,permanent", [("invalid_request", True, True),
                                                    ("transfer_failed", False, True),
                                                    ("transfer_failed", True, False)])
async def test_cli_expected_errors_preserve_safe_message_and_retryability(bridge, monkeypatch, code, retryable, permanent):
    import json

    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(json.dumps({
        "error": {"code": code, "retryable": retryable,
                  "message": "Unsupported Drive folder https://drive.google.com/drive/folders/private?resourcekey=secret. api_key=credential123"},
    }).encode(), b"untrusted stderr secret")))
    monkeypatch.setattr("nanobot.creative_bridge.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(CreativeActionError) as caught:
        await bridge.call("inbox_add", source="https://drive.google.com/drive/folders/private")
    assert caught.value.permanent is permanent
    assert caught.value.code == code
    assert "Unsupported Drive folder" in str(caught.value)
    assert "https://" not in str(caught.value)
    assert "resourcekey" not in str(caught.value)
    assert "credential123" not in str(caught.value)
    assert "stderr" not in str(caught.value)


@pytest.mark.asyncio
async def test_permanent_drive_error_preserves_message_and_successful_assets(bridge):
    source = bridge.media_root / "take.mp4"
    source.write_bytes(b"take")

    async def call(action, **data):
        if action == "inbox_add" and str(data["source"]).startswith("https://"):
            raise CreativeActionError("invalid_request", "Drive folders are unsupported; send an individual public file link.", permanent=True)
        if action == "inbox_add":
            return {"id": 3, "ref": "a" * 64}
        return {"id": 7, "name": "campus"}

    bridge.call = call
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner",
                             content="Remember this folder of takes: https://drive.google.com/drive/folders/reference",
                             media=[str(source)], metadata={"message_id": 21})
    await bridge.handoff(message)
    await bridge.intercept(message)
    with bridge._db() as db:
        row = db.execute("SELECT * FROM receipt").fetchone()
    await bridge._forward(row)
    request = bridge.peers.enqueue.await_args
    assert "Remember this folder of takes" in request.args[1]
    assert request.kwargs["context"]["assets"] == [{"id": 3, "ref": "a" * 64, "role": "raw"}]
    assert "Drive folders are unsupported" in request.kwargs["context"]["input_errors"][0]
    with bridge._db() as db:
        receipt = db.execute("SELECT state,attempts FROM receipt").fetchone()
        assert tuple(receipt) == ("forwarded", 0)
        notices = list(db.execute("SELECT * FROM outbound"))
        assert len(notices) == 1
        assert "individual public file link" in notices[0]["payload"]


@pytest.mark.asyncio
async def test_transient_intake_error_remains_retryable_and_does_not_claim_import(bridge):
    async def call(action, **data):
        if action == "inbox_add":
            raise CreativeActionError("transfer_failed", "Drive is temporarily unavailable.", permanent=False)
        return {"id": 7, "name": "campus"}

    bridge.call = call
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner",
                             content="Use https://drive.google.com/file/d/reference/view", metadata={"message_id": 22})
    await bridge.handoff(message)
    await bridge.intercept(message)
    await bridge.start()
    try:
        for _ in range(30):
            with bridge._db() as db:
                row = db.execute("SELECT state,attempts FROM receipt").fetchone()
            if row["attempts"]:
                break
            await asyncio.sleep(0.01)
        assert tuple(row) == ("queued", 1)
        bridge.peers.enqueue.assert_not_awaited()
        with bridge._db() as db:
            assert db.execute("SELECT count(*) FROM outbound").fetchone()[0] == 0
    finally:
        await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2, 33])
async def test_drive_folder_forwards_all_imported_assets(bridge, count):
    expected = [{"id": i + 3, "ref": f"{i:064x}", "role": "raw"} for i in range(count)]

    async def call(action, **data):
        if action == "inbox_add":
            return {"assets": expected, "destination": "/data/drive/folder"}
        return {"id": 7, "name": "campus"}

    bridge.call = call
    message = InboundMessage(channel="telegram", chat_id="123", sender_id="owner",
                             content="Import https://drive.google.com/drive/folders/reference",
                             metadata={"message_id": 81})
    await bridge.handoff(message)
    await bridge.intercept(message)
    with bridge._db() as db:
        row = db.execute("SELECT * FROM receipt").fetchone()
    await bridge._forward(row)
    assert bridge.peers.enqueue.await_args.kwargs["context"]["assets"] == expected[:32]
    if count > 32:
        assert "Imported 33 files" in bridge.peers.enqueue.await_args.kwargs["context"]["input_errors"][0]


def _outbox_file(tmp_path, name="reel.mp4"):
    f = tmp_path / "data" / "creative-outbox" / "tok" / name
    f.parent.mkdir(parents=True)
    f.write_bytes(b"video")
    return f


def _route():
    return {"channel": "telegram", "chat_id": "123", "message_thread_id": 4}


def _wire_channel(bridge, send):
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]), MessageBus())
    channel.send_registered = send
    bridge.get_channel = lambda _: channel
    bridge._validate_destination = lambda route: None


@pytest.mark.asyncio
async def test_delivery_report_travels_as_the_file_caption(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    ids = await bridge._send({"route": _route(), "path": str(f), "content": "Reel 5 ready. Not yet checked."})
    assert ids == [5]
    msg = send.await_args.args[0]
    assert msg.media == [str(f.resolve())]
    assert msg.metadata["media_caption"].startswith("Reel 5 ready. Not yet checked.")
    assert "Not checked: caption legibility" in msg.metadata["media_caption"]
    assert msg.metadata["content_as_caption"] is True


@pytest.mark.asyncio
async def test_long_report_is_sent_as_text_not_a_clipped_caption(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    await bridge._send({"route": _route(), "path": str(f), "content": "x" * 1500})
    assert "media_caption" not in send.await_args.args[0].metadata


@pytest.mark.asyncio
async def test_unfittable_video_falls_back_to_the_download_link(bridge, tmp_path):
    from nanobot.channels.telegram.media_send import MediaTooLargeError

    f = _outbox_file(tmp_path)
    send = AsyncMock(side_effect=[MediaTooLargeError("reel.mp4 is 900.0 MB, over the limit"), [6]])
    _wire_channel(bridge, send)
    ids = await bridge._send({"route": _route(), "path": str(f), "content": "Done",
                              "url": "https://files.example/x"})
    assert ids == [6]
    fallback = send.await_args_list[1].args[0]
    assert fallback.media == [] and "Original file: https://files.example/x" in fallback.content
    assert "Could not attach the file inline" in fallback.content
    assert "media_caption" not in fallback.metadata


@pytest.mark.asyncio
async def test_unfittable_video_without_a_link_fails_the_delivery(bridge, tmp_path):
    from nanobot.channels.telegram.media_send import MediaTooLargeError

    f = _outbox_file(tmp_path)
    _wire_channel(bridge, AsyncMock(side_effect=MediaTooLargeError("too big")))
    with pytest.raises(ValueError, match="too big"):  # ValueError => the delivery is marked failed
        await bridge._send({"route": _route(), "path": str(f), "content": "Done"})


@pytest.mark.asyncio
async def test_recent_deliveries_are_compact_and_never_block_intake(bridge):
    bridge.call = AsyncMock(return_value={"outputs": [
        {"id": 12, "filename": "a.mp4", "status": "sent", "bytes": 1_807_664,
         "content": "Ready. token=abc123 https://x.example/f", "created_at": "t"},
        *[{"id": i, "filename": f"o{i}.mp4", "status": "sent", "bytes": 1} for i in range(11, 5, -1)],
    ]})
    rows = await bridge._recent_deliveries(2)
    assert len(rows) == 5 and rows[0]["file"] == "a.mp4" and rows[0]["mb"] == 1.8
    assert "abc123" not in rows[0]["report"] and "x.example" not in rows[0]["report"]
    bridge.call = AsyncMock(side_effect=RuntimeError("wonhee down"))
    assert await bridge._recent_deliveries(2) == []


# --- spec 014: contract, change-detected polling, status, stalled jobs -------------------------------

def _contract(**over):
    value = {
        "contract_version": 1, "revision": "abc123",
        "actions": ["contract", "delivery_retry", "delivery_show", "health", "inbox_add", "inbox_list",
                    "outbox_list", "project_create", "project_select", "project_show"],
        "shapes": {"project_create": ["id", "name"], "project_show": ["id", "name"],
                   "inbox_list": ["assets"], "outbox_list": ["outputs"],
                   "delivery_show": ["deliveries", "failures"], "delivery_retry": ["id", "status"],
                   "health": ["stuck_deliveries", "active_jobs"], "inbox_add": [], "project_select": []},
    }
    value.update(over)
    return value


def test_a_compatible_contract_has_no_problems():
    from nanobot.creative_bridge import check_contract
    assert check_contract(_contract()) == []


def test_contract_problems_name_the_exact_drift():
    from nanobot.creative_bridge import check_contract
    contract = _contract(contract_version=2)
    contract["actions"].remove("outbox_list")
    contract["shapes"]["delivery_show"] = ["deliveries"]
    problems = check_contract(contract)
    assert any("version 2" in p for p in problems)
    assert any("no 'outbox_list' action" in p for p in problems)
    assert any("'delivery_show' no longer promises ['failures']" in p for p in problems)


@pytest.mark.asyncio
async def test_verify_contract_records_ok_and_mismatch_and_old_wonhee(bridge):
    bridge.call = AsyncMock(return_value=_contract())
    assert await bridge.verify_contract() is True
    assert bridge.contract_state["ok"] is True and bridge.contract_state["revision"] == "abc123"

    bridge.call = AsyncMock(return_value=_contract(contract_version=9))
    assert await bridge.verify_contract() is False
    assert bridge.contract_state["ok"] is False and bridge.health()["contract"]["problems"]

    bridge.call = AsyncMock(side_effect=CreativeActionError("action_failed", "x", permanent=False))
    assert await bridge.verify_contract() is False  # an old wonhee without the action never raises
    assert "no 'contract' action" in bridge.contract_state["problems"][0]


def test_change_signal_follows_the_marker_wonhee_advertises(bridge, tmp_path):
    import os
    home = tmp_path / "data"
    home.mkdir(exist_ok=True)
    bridge.contract_state = {"ok": True, "problems": [], "signal": ".bridge-signal"}
    assert bridge._wonhee_changed() is True    # first look always polls
    assert bridge._wonhee_changed() is False   # no marker yet: nothing has happened
    marker = home / ".bridge-signal"
    marker.touch()
    assert bridge._wonhee_changed() is True    # wonhee signalled
    assert bridge._wonhee_changed() is False
    os.utime(marker, ns=(5, 5))
    assert bridge._wonhee_changed() is True    # signalled again
    assert bridge._wonhee_changed() is False


def test_without_an_advertised_marker_every_tick_polls_like_before(bridge):
    bridge.contract_state = {"ok": False, "problems": ["old wonhee"], "signal": None}
    assert bridge._wonhee_changed() is True
    assert bridge._wonhee_changed() is True


@pytest.mark.asyncio
async def test_the_signal_is_only_trusted_from_a_compatible_contract(bridge):
    bridge.call = AsyncMock(return_value=_contract(signal=".bridge-signal"))
    await bridge.verify_contract()
    assert bridge.contract_state["signal"] == ".bridge-signal"
    bridge.call = AsyncMock(return_value=_contract(contract_version=9, signal=".bridge-signal"))
    await bridge.verify_contract()
    assert bridge.contract_state["signal"] is None  # a mismatching wonhee is polled the slow, safe way


def test_poll_is_due_on_change_or_after_the_safety_interval(bridge, monkeypatch):
    from nanobot import creative_bridge
    bridge._wonhee_changed = lambda: False
    bridge._last_poll = 1000.0
    monkeypatch.setattr(creative_bridge.time, "monotonic", lambda: 1000.0 + creative_bridge.SAFETY_POLL_S - 1)
    assert bridge._poll_due() is False
    monkeypatch.setattr(creative_bridge.time, "monotonic", lambda: 1000.0 + creative_bridge.SAFETY_POLL_S + 1)
    assert bridge._poll_due() is True
    bridge._wonhee_changed = lambda: True
    assert bridge._poll_due() is True


@pytest.mark.asyncio
async def test_failed_reel_notice_carries_the_real_reason(bridge):
    queued = []

    async def queue_text(identity, content, route, project_id=None):
        queued.append((identity, content))

    bridge.queue_text = queue_text
    bridge.get_channel = lambda _: SimpleNamespace(_app_ready=asyncio.Event())
    bridge.call = AsyncMock(return_value={"deliveries": [], "failures": [
        {"id": 5, "project_id": 7, "route": _route(), "error": "render input could not decode"},
        {"id": 6, "project_id": 7, "route": _route(), "error": None},
    ]})
    await bridge._poll_deliveries()
    assert "render input could not decode" in queued[0][1] and queued[0][0] == "submission-failed:5"
    assert queued[1][1].startswith("Your reel couldn't finish. ")


@pytest.mark.asyncio
async def test_stalled_job_is_announced_once_and_healthy_jobs_are_not(bridge):
    bridge.call = AsyncMock(return_value={"active_jobs": [
        {"id": 3, "project_id": 7, "status": "running", "phase": "render", "idle_s": 1500,
         "stalled": True, "route": _route()},
        {"id": 4, "project_id": 7, "status": "running", "phase": "render", "idle_s": 5,
         "stalled": False, "route": _route()},
    ]})
    await bridge._check_jobs()
    await bridge._check_jobs()
    with bridge._db() as db:
        rows = list(db.execute("SELECT id, payload FROM outbound"))
    assert [r["id"] for r in rows] == ["job-stalled:3"]  # idempotent, and job 4 is healthy
    assert "25 min" in rows[0]["payload"] and "render" in rows[0]["payload"]


@pytest.mark.asyncio
async def test_status_report_lists_stuck_deliveries_jobs_and_contract(bridge):
    bridge.contract_state = {"ok": True, "problems": [], "version": 1, "revision": "abc123"}
    bridge.call = AsyncMock(return_value={
        "stuck_deliveries": [{"id": 9, "filename": "reel.mp4", "status": "uncertain", "age_s": 1200,
                              "error": "delivery interrupted before acknowledgement", "route": _route()}],
        "active_jobs": [{"id": 3, "status": "running", "phase": "render", "idle_s": 1500, "stalled": True}],
    })
    text = await bridge.status_report(_route())
    assert "Contract v1 ok, wonhee abc123" in text
    assert "Delivery 9 reel.mp4: uncertain, 20 min" in text
    assert "Job 3: running in render, idle 25 min - STALLED" in text


@pytest.mark.asyncio
async def test_status_report_says_when_the_contract_is_broken_or_wonhee_is_unreachable(bridge):
    bridge.contract_state = {"ok": False, "problems": ["wonhee has no 'outbox_list' action"]}
    bridge.call = AsyncMock(side_effect=RuntimeError("boom"))
    text = await bridge.status_report(_route())
    assert "CONTRACT MISMATCH: wonhee has no 'outbox_list' action" in text
    assert "Could not read wonhee health" in text


@pytest.mark.asyncio
async def test_retry_requeues_the_newest_stuck_delivery_for_this_chat_only(bridge):
    calls = []

    async def call(action, **data):
        calls.append((action, data))
        if action == "health":
            return {"stuck_deliveries": [
                {"id": 11, "filename": "other.mp4", "status": "uncertain", "route": {"chat_id": "999"}},
                {"id": 10, "filename": "mine.mp4", "status": "uncertain", "route": {"chat_id": "123"}},
                {"id": 9, "filename": "old.mp4", "status": "failed", "route": {"chat_id": "123"}},
                {"id": 8, "filename": "busy.mp4", "status": "sending", "route": {"chat_id": "123"}},
            ]}
        return {"id": data.get("delivery_id"), "status": "queued"}

    bridge.call = call
    assert await bridge._retry_wonhee_delivery(_route()) == "mine.mp4"
    assert ("delivery_retry", {"delivery_id": 10}) in calls
    bridge.call = AsyncMock(return_value={"stuck_deliveries": []})
    assert await bridge._retry_wonhee_delivery(_route()) is None


@pytest.mark.asyncio
async def test_context_lists_only_this_projects_jobs(bridge):
    bridge.call = AsyncMock(return_value={"active_jobs": [
        {"id": 1, "project_id": 7, "status": "running", "phase": "p", "idle_s": 3, "stalled": False, "route": {}},
        {"id": 2, "project_id": 8, "status": "queued", "phase": None, "idle_s": 9, "stalled": False, "route": {}},
    ]})
    assert [j["id"] for j in await bridge._active_jobs(7)] == [1]
    assert "route" not in (await bridge._active_jobs(7))[0]
    bridge.call = AsyncMock(side_effect=RuntimeError("down"))
    assert await bridge._active_jobs(7) == []


# --- spec 014: self-check gate on delivery ------------------------------------------------------------

def _sha(path):
    from nanobot.utils.media_probe import file_sha256
    return file_sha256(path)


@pytest.mark.asyncio
async def test_identical_rerender_is_flagged_in_the_caption_and_recorded(bridge, tmp_path):
    """The Oct 26 failure: reel-5 was byte-identical to reel-4 yet was reported as a fix."""
    f = _outbox_file(tmp_path, "reel-5.mp4")
    sha = _sha(f)
    bridge.call = AsyncMock(return_value={"outputs": [
        {"id": 11, "ref": sha, "filename": "reel-5.mp4", "created_at": "t2"},
        {"id": 10, "ref": sha, "filename": "reel-4.mp4", "created_at": "t1"},
        {"id": 9, "ref": "f" * 64, "filename": "other.mp4", "created_at": "t0"},
    ]})
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    await bridge._send({"id": 11, "project_id": 7, "route": _route(), "path": str(f),
                        "content": "Your reel is ready. Full file check: not yet run."})
    caption = send.await_args.args[0].metadata["media_caption"]
    assert "WARNING: byte-identical to delivery 10 (reel-4.mp4)" in caption
    assert "did not take effect" in caption
    assert "Full file check: not yet run" not in caption  # replaced by what was really measured
    assert f"File {sha[:8]}" in caption and "Not checked:" in caption
    with bridge._db() as db:
        row = db.execute("SELECT * FROM delivery_check WHERE delivery_id=11").fetchone()
    assert row["sha"] == sha and row["duplicate_of"] == 10


@pytest.mark.asyncio
async def test_a_changed_output_is_not_flagged(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    bridge.call = AsyncMock(return_value={"outputs": [
        {"id": 11, "ref": _sha(f), "filename": "new.mp4"}, {"id": 10, "ref": "e" * 64, "filename": "old.mp4"},
    ]})
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    await bridge._send({"id": 11, "project_id": 7, "route": _route(), "path": str(f), "content": "Ready."})
    assert "WARNING" not in send.await_args.args[0].metadata["media_caption"]


@pytest.mark.asyncio
async def test_only_an_earlier_delivery_counts_as_the_original(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    sha = _sha(f)
    bridge.call = AsyncMock(return_value={"outputs": [
        {"id": 12, "ref": sha, "filename": "later.mp4"}, {"id": 11, "ref": sha, "filename": "this.mp4"},
    ]})
    check = await bridge._verify_output({"id": 11, "project_id": 7}, f)
    assert check["duplicate_of"] is None  # delivery 12 came after, so 11 is the first of its kind


@pytest.mark.asyncio
async def test_a_failing_check_never_blocks_the_delivery(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    bridge._verify_output = AsyncMock(side_effect=RuntimeError("probe exploded"))
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    assert await bridge._send({"id": 11, "project_id": 7, "route": _route(), "path": str(f),
                               "content": "Ready."}) == [5]
    assert send.await_args.args[0].metadata["media_caption"] == "Ready."  # unaugmented, but delivered


@pytest.mark.asyncio
async def test_a_wonhee_outage_still_yields_facts_and_the_unchecked_list(bridge, tmp_path):
    f = _outbox_file(tmp_path)
    bridge.call = AsyncMock(side_effect=RuntimeError("wonhee down"))
    check = await bridge._verify_output({"id": 11, "project_id": 7}, f)
    assert check["duplicate_of"] is None and check["sha"] == _sha(f)


@pytest.mark.asyncio
async def test_recent_deliveries_carry_the_check_so_the_agent_cannot_miss_a_duplicate(bridge, tmp_path):
    import json
    with bridge._db() as db:
        db.execute("INSERT INTO delivery_check VALUES(?,?,?,?,?)",
                   (11, "a" * 64, 10, json.dumps({"width": 1080, "height": 1920, "duration_s": 20.3,
                                                  "fps": 30.0, "has_audio": False}), 0.0))
    bridge.call = AsyncMock(return_value={"outputs": [
        {"id": 11, "filename": "reel-5.mp4", "status": "sent", "bytes": 1_800_000, "content": "ok"},
        {"id": 3, "filename": "old.mp4", "status": "sent", "bytes": 1},
    ]})
    rows = await bridge._recent_deliveries(7)
    assert rows[0]["sha"] == "a" * 8 and rows[0]["duplicate_of"] == 10
    assert rows[0]["measured"] == "1080x1920, 20.3 s, 30.0 fps, no audio"
    assert rows[1]["checked"] is False and "sha" not in rows[1]


@pytest.mark.asyncio
async def test_real_ffprobe_facts_land_in_the_report(bridge, tmp_path):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    f = _outbox_file(tmp_path, "real.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=320x240:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(f)], check=True)
    bridge.call = AsyncMock(return_value={"outputs": []})
    send = AsyncMock(return_value=[5])
    _wire_channel(bridge, send)
    await bridge._send({"id": 4, "project_id": 7, "route": _route(), "path": str(f), "content": "Ready."})
    caption = send.await_args.args[0].metadata["media_caption"]
    assert "320x240" in caption and "2.0 s" in caption and "LUFS" in caption
