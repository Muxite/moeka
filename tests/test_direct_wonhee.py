"""Direct conversation, authenticated provenance and bounded failure behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.wonhee_handoff import WonheeHandoffTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import CreativeBridgeConfig
from nanobot.creative_bridge import CreativeBridge
from nanobot.creative_context import register, stamp, validate


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr("nanobot.creative_bridge.get_media_dir", lambda _: media)
    bridge = CreativeBridge(
        CreativeBridgeConfig(
            enabled=True,
            mode="direct",
            python="/usr/bin/python3",
            cwd=str(tmp_path),
            home=str(tmp_path / "data"),
        ),
        workspace=tmp_path / "workspace",
        bus=MessageBus(),
        peers=None,
        get_channel=lambda _: None,
    )
    selected = {"id": 7}

    async def call(action, **data):
        if action == "project_create":
            selected["id"] = 8 if data["name"] == "new" else 7
        return {
            "id": selected["id"],
            "name": "Mesh",
            "brief": {"content": "The original full brief"},
            "assets": [],
        }

    bridge.call = AsyncMock(side_effect=call)
    return bridge


@pytest.mark.asyncio
async def test_direct_ingress_uses_original_telegram_turn_and_no_peer(bridge):
    msg = InboundMessage(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        content="Build this",
        metadata={"message_id": 9},
    )
    await bridge.handoff(msg, project_name="mesh")
    assert await bridge.intercept(msg) is False
    assert msg.channel == "telegram" and msg.session_key == "telegram:123"
    assert "The original full brief" in msg.content
    assert (
        validate(msg.metadata, channel="telegram", chat_id="123", sender_id="owner")["project_id"]
        == 7
    )
    assert bridge.bus.inbound.empty()
    tampered = dict(msg.metadata)
    tampered["creative"] = {**tampered["creative"], "project_id": 999}
    with pytest.raises(ValueError):
        validate(tampered, channel="telegram", chat_id="123", sender_id="owner")


@pytest.mark.asyncio
async def test_project_selection_updates_same_turn_receipt_and_context(bridge):
    msg = InboundMessage(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        content="Create a new mesh project and build it",
        metadata={"message_id": 9},
    )
    await bridge.handoff(msg, project_name="mesh")
    await bridge.intercept(msg)
    ctx = RequestContext(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        original_user_text="Create a new mesh project and build it",
        metadata=dict(msg.metadata),
    )
    tool = WonheeHandoffTool(bridge)
    assert tool.name == "wonhee_project"
    with request_context(ctx):
        await tool.execute(project_name="new")
        assert (
            validate(ctx.metadata, channel="telegram", chat_id="123", sender_id="owner")[
                "project_id"
            ]
            == 8
        )
    with bridge._db() as db:
        assert db.execute("SELECT project_id FROM receipt").fetchone()[0] == 8
    assert any(call.args[0] == "project_brief_save" for call in bridge.call.await_args_list)


def test_host_proofs_survive_restart_registration(bridge):
    context = {
        "version": 1,
        "project_id": 7,
        "request_id": "request",
        "route": {
            "channel": "telegram",
            "chat_id": "123",
            "owner_id": "owner",
            "route_key": "telegram:123:0:owner",
        },
    }
    metadata = {}
    stamp(metadata, context, bridge.context_key)
    assert register(bridge.workspace) == bridge.context_key
    assert (
        validate(metadata, channel="telegram", chat_id="123", sender_id="owner")["project_id"] == 7
    )
    with pytest.raises(ValueError):
        validate({"creative": context}, channel="telegram", chat_id="123", sender_id="owner")


@pytest.mark.asyncio
async def test_repeated_permanent_mcp_failure_requests_turn_stop():
    payload = {"error": {"code": "invalid_request", "message": "missing brief", "retryable": False}}
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(
                isError=True,
                structuredContent=payload,
                content=[TextContent(type="text", text="missing brief")],
            )
        )
    )
    tool = MCPToolWrapper(
        session,
        "wonhee",
        Tool(name="stagecraft_build", inputSchema={"type": "object"}),
        transport="stdio",
    )
    with request_context(RequestContext(channel="telegram", chat_id="123")):
        first = await tool.execute(brief="")
        second = await tool.execute(brief="")
    assert first.is_error and not first.metadata.get("stop_turn")
    assert second.metadata["stop_turn"] is True


@pytest.mark.asyncio
async def test_explicit_switch_overrides_reply_to_old_project(bridge):
    msg = InboundMessage(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        content="Create a new project",
        metadata={"message_id": 10, "reply_to_message_id": 99},
    )
    await bridge.handoff(msg, project_name="mesh")
    with bridge._db() as db:
        db.execute(
            "INSERT INTO delivered(chat_id,message_id,project_id) VALUES(?,?,?)", ("123", 99, 7)
        )
    await bridge.intercept(msg)
    ctx = RequestContext(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        original_user_text="Create a new project",
        metadata=dict(msg.metadata),
    )
    with request_context(ctx):
        await WonheeHandoffTool(bridge).execute(project_name="new")
    assert (
        validate(ctx.metadata, channel="telegram", chat_id="123", sender_id="owner")["project_id"]
        == 8
    )


@pytest.mark.asyncio
async def test_direct_project_without_saved_brief_is_valid(bridge):
    original = bridge.call.side_effect

    async def without_brief(action, **data):
        result = await original(action, **data)
        result.pop("brief", None)
        return result

    bridge.call.side_effect = without_brief
    msg = InboundMessage(
        channel="telegram",
        chat_id="123",
        sender_id="owner",
        content="Build this",
        metadata={"message_id": 11},
    )
    await bridge.handoff(msg, project_name="mesh")
    assert await bridge.intercept(msg) is False
    assert '"brief": {}' in msg.content
