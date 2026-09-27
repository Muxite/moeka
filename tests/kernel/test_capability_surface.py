"""Static capability surface: every name ``Tool.capabilities`` can ever return (I4).

``Tool.capability_surface`` is what sub-agent attenuation requests for a child's tool
registry; the tests below keep it in sync with Task 8's per-call mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.loader import ToolLoader, _LegacyErrorPrefixTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.policy import CapabilityRequest
from nanobot.session.manager import SessionManager
from tests.agent.test_registered_tool_names import EXPECTED_SLIM_TOOLS
from tests.kernel.test_tool_capabilities import EXPECTED


@pytest.fixture
def registered_tools(tmp_path) -> dict[str, Tool]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        bus=MessageBus(),
        subagent_manager=SimpleNamespace(get_running_count=lambda: 0,
                                         max_concurrent_subagents=4),
        exec_session_manager=ExecSessionManager(),
        sessions=SessionManager(workspace, sessions_root=tmp_path / "runtime"),
        timezone="UTC",
    )
    registry = ToolRegistry()
    ToolLoader().load(ctx, registry)
    return {name: registry.get(name) for name in registry.tool_names}


# name -> the static capability surface each built-in tool declares.
EXPECTED_SURFACE: dict[str, frozenset[str]] = {
    "apply_patch": frozenset({"fs.read", "fs.write"}),
    "ask_user": frozenset(),
    "create_goal": frozenset(),
    "edit_file": frozenset({"fs.write"}),
    "exec": frozenset({"exec.run"}),
    "exec_session": frozenset({"exec.session_input"}),
    "find_files": frozenset({"fs.read"}),
    "grep": frozenset({"fs.read"}),
    "list_dir": frozenset({"fs.read"}),
    "list_exec_sessions": frozenset(),
    "list_sessions": frozenset({"session.read"}),
    "read_file": frozenset({"fs.read"}),
    "read_session": frozenset({"session.read"}),
    "search_sessions": frozenset({"session.read"}),
    "send_session_message": frozenset({"session.send"}),
    "spawn": frozenset({"budget.subagents"}),
    "update_goal": frozenset(),
    "web_fetch": frozenset({"net.fetch"}),
    "web_search": frozenset({"net.fetch"}),
    "write_file": frozenset({"fs.write"}),
}


def test_surface_mapping_covers_the_registered_set():
    assert sorted(EXPECTED_SURFACE) == sorted(EXPECTED_SLIM_TOOLS)


@pytest.mark.parametrize("name", EXPECTED_SLIM_TOOLS)
def test_every_builtin_declares_its_static_surface(name, registered_tools):
    assert registered_tools[name].capability_surface() == EXPECTED_SURFACE[name]


@pytest.mark.parametrize("name", EXPECTED_SLIM_TOOLS)
def test_surface_covers_every_task8_declaration(name, registered_tools):
    """Kept in sync with Task 8's per-call mapping: no call escapes the surface."""
    surface = registered_tools[name].capability_surface()
    for params, _ in EXPECTED[name]:
        got = {r.capability for r in registered_tools[name].capabilities(params)}
        assert got <= surface, f"{name} {params!r}"


def test_mcp_and_bg_shell_and_image_surfaces():
    from nanobot.agent.tools.bg_shell import BackgroundShellTool
    from nanobot.agent.tools.image_generation import ImageGenerationTool
    from nanobot.agent.tools.mcp import MCPPromptWrapper, MCPResourceWrapper, MCPToolWrapper

    for cls in (MCPToolWrapper, MCPResourceWrapper, MCPPromptWrapper):
        assert cls.__new__(cls).capability_surface() == frozenset({"mcp.call", "net.fetch"})
    bg = BackgroundShellTool.__new__(BackgroundShellTool)
    assert bg.capability_surface() == frozenset({"exec.run", "exec.session_input"})
    img = ImageGenerationTool.__new__(ImageGenerationTool)
    assert img.capability_surface() == frozenset({"fs.read", "net.fetch"})


class _Plain(Tool):
    name = "plain"  # pyright: ignore[reportAssignmentType]
    description = "plain"  # pyright: ignore[reportAssignmentType]
    parameters = {"type": "object", "properties": {}}  # pyright: ignore[reportAssignmentType]

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _Undeclared(_Plain):
    name = "undeclared"  # pyright: ignore[reportAssignmentType]

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest("mcp.call", "x")]


class _Declared(_Plain):
    name = "declared"  # pyright: ignore[reportAssignmentType]
    _capability_names = frozenset({"net.fetch"})

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest("net.fetch", "x")]


class _StaleOverride(_Declared):
    """Overrides capabilities() but inherits the parent's names: not trusted."""

    name = "stale"  # pyright: ignore[reportAssignmentType]

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest("exec.run", "x")]


def test_surface_default_undeclared_and_stale():
    assert _Plain().capability_surface() == frozenset()
    assert _Undeclared().capability_surface() is None
    assert _Declared().capability_surface() == frozenset({"net.fetch"})
    assert _StaleOverride().capability_surface() is None


def test_legacy_wrapper_forwards_surface():
    assert _LegacyErrorPrefixTool(_Declared()).capability_surface() == frozenset({"net.fetch"})
    assert _LegacyErrorPrefixTool(_Undeclared()).capability_surface() is None
