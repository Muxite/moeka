"""Native host tmux grants are explicit and independent of sandboxed shell access."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.tmux import TmuxTool
from nanobot.config.schema import TmuxToolConfig, ToolsConfig


@pytest.fixture
def server(tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = tmp_path / "tmux.sock"
    subprocess.run(["tmux", "-f", "/dev/null", "-S", str(socket), "new-session", "-d", "-s", "allowed", "/bin/sh"], check=True)
    subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s", "private", "/bin/sh"], check=True)
    yield socket
    subprocess.run(["tmux", "-S", str(socket), "kill-server"], check=False, capture_output=True)


def tool(socket, sessions=None):
    return TmuxTool(TmuxToolConfig(enable=True, socket_path=str(socket), allowed_sessions=sessions or ["allowed"]))


async def wait_for_line(client, pane, expected):
    for _ in range(40):
        result = await client.execute("read", target=pane)
        if expected in result.splitlines():
            return result
        await asyncio.sleep(0.05)
    pytest.fail(f"missing terminal output: {expected}")


def test_config_requires_explicit_socket_and_sessions():
    assert not TmuxToolConfig().enable
    with pytest.raises(ValidationError, match="absolute"):
        TmuxToolConfig(enable=True, socket_path="relative")
    with pytest.raises(ValidationError, match="allowed_sessions"):
        TmuxToolConfig(enable=True, socket_path="/tmp/socket")


def test_discovery_and_host_only_enable(tmp_path):
    assert TmuxTool in ToolLoader().discover()
    config = ToolsConfig()
    ctx = ToolContext(config=config, workspace=str(tmp_path), host_tools=True)
    assert not TmuxTool.enabled(ctx)
    config.tmux = TmuxToolConfig(enable=True, socket_path="/tmp/socket", allowed_sessions=["*"])
    assert TmuxTool.enabled(ctx)
    registry = ToolRegistry()
    assert ToolLoader(test_classes=[TmuxTool]).load(ctx, registry) == ["tmux"]
    assert registry.has("tmux")
    ctx.host_tools = False
    assert not TmuxTool.enabled(ctx)


@pytest.mark.asyncio
async def test_real_new_window_read_and_literal_input(server):
    client = tool(server)
    created = await client.execute("new_window", target="allowed", name="native-test")
    assert not created.is_error, created
    pane = json.loads(created)["pane"]
    panes = json.loads(await client.execute("list"))["panes"]
    assert any(p["pane"] == pane for p in panes)
    assert all(p["session"] == "allowed" for p in panes)
    # Split marker across arguments so matching terminal output cannot be the shell's input echo.
    sent = await client.execute("send", target=pane, text="printf 'native-%s\\n' 'tmux-ok'", enter=True)
    assert not sent.is_error, sent
    await wait_for_line(client, pane, "native-tmux-ok")
    # Input is a buffer payload, never tmux command arguments, including semicolons and Unicode.
    sent = await client.execute("send", target=pane, text="printf '%s\\n' 'literal ; café'", enter=True)
    assert not sent.is_error, sent
    await wait_for_line(client, pane, "literal ; café")
    buffers = subprocess.run(["tmux", "-S", str(server), "list-buffers"], capture_output=True, text=True, check=True)
    assert "moeka-" not in buffers.stdout


@pytest.mark.asyncio
async def test_send_without_enter_does_not_execute(server):
    client = tool(server)
    pane = json.loads(await client.execute("new_window", target="allowed"))["pane"]
    await client.execute("send", target=pane, text="printf 'deferred-%s\\n' 'ok'")
    result = await client.execute("read", target=pane)
    assert "deferred-ok" not in result.splitlines()
    await client.execute("send", target=pane, key="Enter")
    await wait_for_line(client, pane, "deferred-ok")


@pytest.mark.asyncio
async def test_private_panes_and_ambiguous_targets_refused(server):
    client = tool(server)
    private = subprocess.run(["tmux", "-S", str(server), "display-message", "-p", "-t", "private:", "#{pane_id}"],
                             capture_output=True, text=True, check=True).stdout.strip()
    for action in ("read", "send"):
        result = await client.execute(action, target=private, text="should not send", enter=True)
        assert result.is_error and "outside the allowed" in result
        result = await client.execute(action, target="allowed:0")
        assert result.is_error and "exact %pane" in result
    result = await client.execute("new_window", target="private")
    assert result.is_error


@pytest.mark.asyncio
async def test_missing_socket_and_disabled_connection(tmp_path):
    missing = await tool(tmp_path / "missing").execute("list")
    assert missing.is_error
    disabled = await TmuxTool(TmuxToolConfig()).execute("list")
    assert disabled.is_error and "disabled" in disabled
    regular = tmp_path / "regular"
    regular.write_text("not a socket")
    assert (await tool(regular).execute("list")).is_error


@pytest.mark.asyncio
async def test_invalid_keys_and_output_bounds(server):
    client = tool(server)
    pane = json.loads(await client.execute("new_window", target="allowed"))["pane"]
    for kwargs in ({"key": "run-shell"}, {"text": "x" * 16385}, {"lines": 1001}):
        assert (await client.execute("send", target=pane, **kwargs)).is_error


@pytest.mark.asyncio
async def test_registry_applies_terminal_policy_and_input_floor(server):
    from nanobot.kernel.policy import DefaultPolicy
    registry = ToolRegistry()
    registry.register(tool(server))
    registry.configure_gate(policy=DefaultPolicy(deny_capabilities=frozenset({"terminal.write"})))
    result = await registry.execute("tmux", {"action": "new_window", "target": "allowed"})
    assert "blocked by permission policy" in result
    assert not (await registry.execute("tmux", {"action": "list"})).is_error
    registry.configure_gate(policy=DefaultPolicy())
    pane = json.loads(await registry.execute("tmux", {"action": "new_window", "target": "allowed"}))["pane"]
    result = await registry.execute("tmux", {"action": "send", "target": pane, "text": ":(){ :|:& };:", "enter": True})
    assert result.is_error
