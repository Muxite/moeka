"""Tests for the background shell tool and registry."""

import asyncio

import pytest

from nanobot.agent.tools.bg_shell import (
    BackgroundProcessRegistry,
    BackgroundShellTool,
)
from nanobot.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_bg_shell_publishes_system_message_on_exit(tmp_path):
    bus = MessageBus()
    registry = BackgroundProcessRegistry(bus=bus, workspace=tmp_path)
    tool = BackgroundShellTool(registry=registry)
    tool.set_context("telegram", "12345", session_key="telegram:12345")

    result = await tool.execute(action="start", command="echo hello world", label="echo")
    assert "Started background task" in result
    assert "pid=" in result

    # Wait for the system-channel inbound to land on the bus.
    msg = await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)
    assert msg.channel == "system"
    assert msg.sender_id == "bg_shell"
    assert msg.chat_id == "telegram:12345"
    assert msg.session_key_override == "telegram:12345"
    assert "Exit code: 0" in msg.content
    assert msg.metadata["_proactive_source"] == "bg_shell"
    assert msg.metadata["_bg_task_id"]


@pytest.mark.asyncio
async def test_bg_shell_tail_returns_recent_log(tmp_path):
    bus = MessageBus()
    registry = BackgroundProcessRegistry(bus=bus, workspace=tmp_path)
    tool = BackgroundShellTool(registry=registry)
    tool.set_context("cli", "direct", session_key="cli:direct")

    start_result = await tool.execute(action="start", command="echo hi from bg")
    # Extract the task id from "Started background task <id> ..."
    task_id = start_result.split()[3]

    # Drain the system-channel announcement so it doesn't leak into other tests.
    await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)

    tail = await tool.execute(action="tail", task_id=task_id)
    assert "hi from bg" in tail
    assert f"task_id={task_id}" in tail


@pytest.mark.asyncio
async def test_bg_shell_list_only_shows_session_tasks(tmp_path):
    bus = MessageBus()
    registry = BackgroundProcessRegistry(bus=bus, workspace=tmp_path)
    tool = BackgroundShellTool(registry=registry)

    tool.set_context("telegram", "111", session_key="telegram:111")
    await tool.execute(action="start", command="true", label="alice")

    tool.set_context("telegram", "222", session_key="telegram:222")
    await tool.execute(action="start", command="true", label="bob")

    # Drain both exit announcements
    await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)
    await asyncio.wait_for(bus.consume_inbound(), timeout=5.0)

    tool.set_context("telegram", "111", session_key="telegram:111")
    listing = await tool.execute(action="list")
    assert "alice" in listing
    assert "bob" not in listing


@pytest.mark.asyncio
async def test_bg_shell_unknown_action_errors(tmp_path):
    bus = MessageBus()
    registry = BackgroundProcessRegistry(bus=bus, workspace=tmp_path)
    tool = BackgroundShellTool(registry=registry)
    tool.set_context("cli", "direct", session_key="cli:direct")
    out = await tool.execute(action="explode")
    assert "unknown action" in out.lower()
