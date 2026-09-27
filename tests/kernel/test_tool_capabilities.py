"""Every built-in tool declares the capabilities a call needs (``Tool.capabilities``).

The main test is parametrised over the canonical registered-tool list, so a new tool
added to the loader without an entry in ``EXPECTED`` fails here until its capability
mapping is written down.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.bg_shell import BackgroundShellTool
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.image_generation import ImageGenerationTool
from nanobot.agent.tools.loader import ToolLoader, _LegacyErrorPrefixTool
from nanobot.agent.tools.mcp import MCPPromptWrapper, MCPResourceWrapper, MCPToolWrapper
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.policy import CapabilityRequest
from nanobot.session.manager import SessionManager
from tests.agent.test_registered_tool_names import EXPECTED_SLIM_TOOLS


def _req(capability: str, resource: str = "") -> CapabilityRequest:
    return CapabilityRequest(capability, resource)


# name -> list of (params, expected capability requests)
EXPECTED: dict[str, list[tuple[dict[str, Any], list[CapabilityRequest]]]] = {
    "apply_patch": [
        (
            {"edits": [
                {"path": "a.txt", "action": "add", "new_text": "x"},
                {"path": "b.txt", "action": "replace", "old_text": "y", "new_text": "z"},
                {"path": "a.txt", "action": "add", "new_text": "w"},
            ]},
            [_req("fs.write", "a.txt"), _req("fs.write", "b.txt")],
        ),
        (
            {"edits": [{"path": "a.txt", "action": "add", "new_text": "x"}], "dry_run": True},
            [_req("fs.read", "a.txt")],
        ),
    ],
    "ask_user": [({"question": "ok?"}, [])],
    "create_goal": [({"objective": "ship it"}, [])],
    "edit_file": [
        ({"path": "src/x.py", "old_text": "a", "new_text": "b"}, [_req("fs.write", "src/x.py")]),
    ],
    "exec": [
        ({"command": "ls -la"}, [_req("exec.run", "ls -la")]),
        ({"cmd": "uptime"}, [_req("exec.run", "uptime")]),
    ],
    "exec_session": [
        ({"session_id": "s1"}, []),
        ({"session_id": "s1", "wait_for": "$ "}, []),
        ({"session_id": "s1", "input": "rm x\n"}, [_req("exec.session_input", "rm x\n")]),
        ({"session_id": "s1", "close_stdin": True}, [_req("exec.session_input", "s1")]),
        ({"session_id": "s1", "terminate": True}, [_req("exec.session_input", "s1")]),
    ],
    "find_files": [
        ({"path": "src", "query": "foo"}, [_req("fs.read", "src")]),
        ({"query": "foo"}, [_req("fs.read", ".")]),
    ],
    "grep": [
        ({"pattern": "TODO", "path": "lib"}, [_req("fs.read", "lib")]),
        ({"pattern": "TODO"}, [_req("fs.read", ".")]),
    ],
    "list_dir": [({"path": "/etc"}, [_req("fs.read", "/etc")])],
    "list_exec_sessions": [({}, [])],
    "list_sessions": [({}, [_req("session.read", "*")])],
    "read_file": [({"path": "/etc/hosts"}, [_req("fs.read", "/etc/hosts")])],
    "read_session": [({"session_key": "@ops"}, [_req("session.read", "@ops")])],
    "search_sessions": [({"query": "nginx"}, [_req("session.read", "*")])],
    "send_session_message": [
        (
            {"to": "@ops", "content": "hi", "expect_reply": False},
            [_req("session.send", "@ops")],
        ),
    ],
    "spawn": [({"task": "do a thing"}, [_req("budget.subagents")])],
    "update_goal": [({"action": "complete"}, [])],
    "web_fetch": [
        ({"url": "https://example.com/a"}, [_req("net.fetch", "https://example.com/a")]),
    ],
    "web_search": [({"query": "linux oom"}, [_req("net.fetch", "search:linux oom")])],
    "write_file": [({"path": "out.txt", "content": "x"}, [_req("fs.write", "out.txt")])],
}


@pytest.fixture
def registered_tools(tmp_path) -> dict[str, Tool]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        bus=MessageBus(),
        subagent_manager=SimpleNamespace(
            get_running_count=lambda: 0,
            max_concurrent_subagents=4,
        ),
        exec_session_manager=ExecSessionManager(),
        sessions=SessionManager(workspace, sessions_root=tmp_path / "runtime"),
        timezone="UTC",
    )
    registry = ToolRegistry()
    ToolLoader().load(ctx, registry)
    return {name: registry.get(name) for name in registry.tool_names}


def test_expected_mapping_covers_exactly_the_registered_set():
    assert sorted(EXPECTED) == sorted(EXPECTED_SLIM_TOOLS)


@pytest.mark.parametrize("name", EXPECTED_SLIM_TOOLS)
def test_registered_tool_declares_capabilities(name, registered_tools):
    assert name in EXPECTED, f"tool {name!r} has no declared capability mapping"
    tool = registered_tools[name]
    for params, expected in EXPECTED[name]:
        got = tool.capabilities(params)
        assert got == expected, f"{name} {params!r}"
        assert all(isinstance(r, CapabilityRequest) for r in got)


def test_base_tool_default_is_no_capabilities():
    class _Plain(Tool):
        name = "plain"  # pyright: ignore[reportAssignmentType]
        description = "plain"  # pyright: ignore[reportAssignmentType]
        parameters = {"type": "object", "properties": {}}  # pyright: ignore[reportAssignmentType]

        async def execute(self, **kwargs: Any) -> str:
            return "ok"

    assert _Plain().capabilities({"anything": 1}) == []


def test_capabilities_tolerate_missing_params(registered_tools):
    """Malformed calls still yield a declaration (never raise) so the gate can run."""
    for name in EXPECTED_SLIM_TOOLS:
        got = registered_tools[name].capabilities({})
        assert isinstance(got, list)


def test_legacy_wrapper_forwards_capabilities(registered_tools):
    wrapped = _LegacyErrorPrefixTool(registered_tools["exec"])
    assert wrapped.capabilities({"command": "id"}) == [_req("exec.run", "id")]


def test_my_tool_declares_none():
    tool = MyTool.__new__(MyTool)
    assert tool.capabilities({"action": "inspect"}) == []
    assert tool.capabilities({"action": "set", "key": "x", "value": 1}) == []


def test_image_generation_declares_net_and_reference_reads():
    tool = ImageGenerationTool.__new__(ImageGenerationTool)
    assert tool.capabilities({"prompt": "a cat"}) == [_req("net.fetch", "image_generation")]
    assert tool.capabilities({"prompt": "a cat", "reference_images": ["a.png", "b.png"]}) == [
        _req("fs.read", "a.png"),
        _req("fs.read", "b.png"),
        _req("net.fetch", "image_generation"),
    ]


def test_bg_shell_start_is_exec_run_and_kill_is_session_input():
    tool = BackgroundShellTool.__new__(BackgroundShellTool)
    assert tool.capabilities({"action": "start", "command": "make"}) == [_req("exec.run", "make")]
    assert tool.capabilities({"action": "kill", "task_id": "t1"}) == [
        _req("exec.session_input", "t1"),
    ]
    assert tool.capabilities({"action": "status", "task_id": "t1"}) == []
    assert tool.capabilities({"action": "list"}) == []


def _session() -> SimpleNamespace:
    return SimpleNamespace(call_tool=None, read_resource=None, get_prompt=None)


def test_mcp_wrappers_declare_mcp_call():
    tool_def = SimpleNamespace(
        name="query", description="q", inputSchema={"type": "object", "properties": {}},
    )
    resource_def = SimpleNamespace(name="docs", uri="file:///docs", description=None)
    prompt_def = SimpleNamespace(name="review", description=None, arguments=[])
    wrappers = [
        (MCPToolWrapper(_session(), "db", tool_def), "db.query"),
        (MCPResourceWrapper(_session(), "db", resource_def), "db.docs"),
        (MCPPromptWrapper(_session(), "db", prompt_def), "db.review"),
    ]
    for wrapper, target in wrappers:
        assert wrapper.capabilities({}) == [_req("mcp.call", target)]


def test_mcp_http_transport_also_declares_net_fetch():
    tool_def = SimpleNamespace(
        name="query", description="q", inputSchema={"type": "object", "properties": {}},
    )
    wrapper = MCPToolWrapper(
        _session(), "remote", tool_def,
        transport="streamableHttp", url="https://user:tok@mcp.example.com/v1/mcp?key=s",
    )
    assert wrapper.capabilities({}) == [
        _req("mcp.call", "remote.query"),
        _req("net.fetch", "https://mcp.example.com/..."),
    ]
    stdio = MCPToolWrapper(_session(), "local", tool_def, transport="stdio")
    assert stdio.capabilities({}) == [_req("mcp.call", "local.query")]


@pytest.mark.parametrize(
    "module",
    ["nanobot.agent.tools.base", "nanobot.agent.tools.shell", "nanobot.agent.tools.mcp"],
)
def test_tool_modules_import_first_without_cycle(module):
    """``base`` must not import ``nanobot.kernel`` at module level (its ``__init__``
    re-exports ``nanobot.core``, which imports ``base`` back)."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
