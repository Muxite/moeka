"""Spec 010 FR-004: host entry points fail startup (exit 2) without the host tools."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.cli.host_tools_guard import (
    HOST_TOOLS,
    expected_host_tools,
    missing_host_tools,
    require_host_tools,
)
from nanobot.config.schema import Config
from nanobot.cron.service import CronService
from nanobot.providers.image_generation import image_gen_provider_configs

REPO = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    return config


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def _gateway_loop(config: Config, *, host_tools: bool = True) -> AgentLoop:
    """Build the loop the way ``nanobot/cli/gateway_runtime.py`` does (minus channels)."""
    bus = MessageBus()
    cron = CronService(config.workspace_path / "cron" / "jobs.json")
    return AgentLoop.from_config(
        config, bus,
        provider=_provider(),
        model="test-model",
        cron_service=cron,
        host_tools=host_tools,
        image_generation_provider_configs=image_gen_provider_configs(config),
        tool_registry=ToolRegistry(),
    )


def test_gateway_loop_registers_the_three_host_tools(tmp_path: Path) -> None:
    config = _config(tmp_path)
    loop = _gateway_loop(config)
    assert set(HOST_TOOLS) <= set(loop.tools.tool_names)
    assert missing_host_tools(loop, config) == []
    require_host_tools(loop, config, entry="nanobot gateway")  # no exit


def test_loop_without_host_tools_fails_with_exit_2(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    loop = _gateway_loop(config, host_tools=False)
    with pytest.raises(typer.Exit) as exc:
        require_host_tools(loop, config, entry="nanobot gateway")
    assert exc.value.exit_code == 2
    err = capsys.readouterr().err
    assert "nanobot gateway" in err
    for name in HOST_TOOLS:
        assert name in err


def test_kernel_loops_still_build_without_host_tools(tmp_path: Path) -> None:
    loop = AgentLoop.from_config(
        _config(tmp_path), provider=_provider(), host_tools=False, tool_registry=ToolRegistry(),
    )
    assert not set(HOST_TOOLS) & set(loop.tools.tool_names)


def test_tools_the_config_turns_off_are_not_expected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert expected_host_tools(config) == list(HOST_TOOLS)
    config.tools.cli_apps.enable = False
    assert expected_host_tools(config) == ["message", "cron"]
    config.agents.defaults.tools_deny = ["cron"]
    assert expected_host_tools(config) == ["message"]
    config.agents.defaults.tools_allow = ["read_file"]
    assert expected_host_tools(config) == []


def test_serve_guard_does_not_expect_cron(tmp_path: Path) -> None:
    """``serve`` builds its loop without a cron service (spec 010 FR-004 as amended)."""
    config = _config(tmp_path)
    loop = AgentLoop.from_config(
        config, MessageBus(), provider=_provider(), host_tools=True,
        tool_registry=ToolRegistry(),
    )
    assert "cron" not in loop.tools.tool_names
    assert expected_host_tools(config, with_cron=False) == ["message", "run_cli_app"]
    require_host_tools(loop, config, entry="nanobot serve", with_cron=False)  # no exit
    with pytest.raises(typer.Exit) as exc:
        require_host_tools(loop, config, entry="nanobot gateway")
    assert exc.value.exit_code == 2


def test_every_host_entry_point_calls_the_guard() -> None:
    """gateway, serve and the CLI agent each check after building their loop."""
    sites = {
        "nanobot/cli/gateway_runtime.py": "nanobot gateway",
        "nanobot/cli/commands.py": "nanobot serve",
        "nanobot/cli/agent.py": "nanobot agent",
    }
    for rel, entry in sites.items():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "require_host_tools"
        ]
        entries = [
            kw.value.value for call in calls for kw in call.keywords
            if kw.arg == "entry" and isinstance(kw.value, ast.Constant)
        ]
        assert entries == [entry], rel
        with_cron = [
            kw.value.value for call in calls for kw in call.keywords if kw.arg == "with_cron"
        ]
        assert with_cron == ([False] if entry == "nanobot serve" else []), rel
