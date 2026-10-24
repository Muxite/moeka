"""Wonhee provenance crosses only an owner-opted-in local MCP transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import ValidationError

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.config.schema import MCPServerConfig


@pytest.fixture
def creative_context():
    return {
        "version": 1, "project_id": 7, "request_id": "request-1",
        "route": {"channel": "telegram", "chat_id": "123", "owner_id": "owner",
                  "message_id": 9, "message_thread_id": 4, "route_key": "telegram:123:4:owner"},
        "assets": [{"id": 2, "ref": "a" * 64, "role": "raw"}],
    }


def wrapper(*, enabled=False, transport="stdio"):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(
        content=[TextContent(type="text", text="saved")],
    )))
    tool = MCPToolWrapper(session, "wonhee", Tool(name="memory_remember", description="Remember",
                          inputSchema={"type": "object", "properties": {"content": {"type": "string"}}}),
                          transport=transport, forward_creative_context=enabled)
    return tool, session


@pytest.mark.asyncio
async def test_opted_in_stdio_forwards_only_validated_creative_context(creative_context):
    tool, session = wrapper(enabled=True)
    ctx = RequestContext(channel="peer", chat_id="delivery", sender_id="paired-peer", metadata={
        "creative": creative_context, "provider_secret": "never-forward", "history": ["private conversation"],
    })
    with request_context(ctx):
        await tool.execute(content="keep the raw video")
    assert session.call_tool.await_args.args == ("memory_remember",)
    assert session.call_tool.await_args.kwargs == {
        "arguments": {"content": "keep the raw video"}, "meta": {"wonhee": creative_context},
    }
    assert "never-forward" not in json.dumps(session.call_tool.await_args.kwargs)
    assert "private conversation" not in json.dumps(session.call_tool.await_args.kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,present", [(False, True), (True, False)])
async def test_disabled_or_absent_context_keeps_legacy_call_signature(creative_context, enabled, present):
    tool, session = wrapper(enabled=enabled)
    ctx = RequestContext(channel="peer", chat_id="delivery", metadata={"creative": creative_context} if present else {})
    with request_context(ctx):
        await tool.execute(content="preference")
    assert session.call_tool.await_args.kwargs == {"arguments": {"content": "preference"}}


@pytest.mark.asyncio
async def test_invalid_context_is_refused_without_exposing_it(creative_context):
    tool, session = wrapper(enabled=True)
    creative_context["assets"][0]["ref"] = "/private/secret-file"
    with request_context(RequestContext(channel="peer", chat_id="delivery", metadata={"creative": creative_context})):
        result = await tool.execute(content="preference")
    assert getattr(result, "is_error", False)
    assert "/private/secret-file" not in result
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_untrusted_channel_cannot_forward_peer_provenance(creative_context):
    tool, session = wrapper(enabled=True)
    with request_context(RequestContext(channel="websocket", chat_id="client", metadata={"creative": creative_context})):
        result = await tool.execute(content="preference")
    assert getattr(result, "is_error", False)
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_arguments_do_not_become_request_metadata():
    tool, session = wrapper(enabled=True)
    with request_context(RequestContext(channel="peer", chat_id="delivery")):
        await tool.execute(content="preference", meta={"wonhee": {"project_id": 999}})
    assert "meta" not in session.call_tool.await_args.kwargs
    assert session.call_tool.await_args.kwargs["arguments"]["meta"]["wonhee"]["project_id"] == 999


@pytest.mark.parametrize("server_type", ["sse", "streamableHttp"])
async def test_creative_context_forwarding_refuses_remote_transports(server_type):
    with pytest.raises(ValidationError, match="stdio"):
        MCPServerConfig(type=server_type, url="https://example.com/mcp", forwardCreativeContext=True)
    with pytest.raises(ValueError, match="stdio"):
        wrapper(enabled=True, transport=server_type)


def test_creative_context_flag_uses_owner_config_camelcase():
    assert MCPServerConfig(command="python", forwardCreativeContext=True).forward_creative_context
    assert not MCPServerConfig(command="python").forward_creative_context
    with pytest.raises(ValidationError, match="stdio"):
        MCPServerConfig(url="https://example.com/mcp", forwardCreativeContext=True)


@pytest.mark.asyncio
async def test_reconnect_cannot_carry_stdio_opt_in_to_remote_replacement(creative_context):
    tool, original = wrapper(enabled=True)
    original.call_tool.side_effect = RuntimeError("Session terminated")
    replacement, remote = wrapper(enabled=False, transport="streamableHttp")
    tool.set_reconnect_handler(AsyncMock(return_value=replacement))
    with request_context(RequestContext(channel="peer", chat_id="delivery", metadata={"creative": creative_context})):
        await tool.execute(content="preference")
    assert "meta" in original.call_tool.await_args.kwargs
    assert remote.call_tool.await_args.kwargs == {"arguments": {"content": "preference"}}
