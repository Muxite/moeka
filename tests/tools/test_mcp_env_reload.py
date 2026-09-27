"""MCP hot reload reads the host ConfigSource (Task 5); COMSPEC from the host base env."""

from __future__ import annotations

import pytest

from nanobot.agent.tools import mcp as mcp_mod
from nanobot.agent.tools.mcp import MCPProvider, _normalize_windows_stdio_command
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.kernel.legacy import LegacyEnvironment
from tests._kernel_env import DictConfigSource, credential_env


def _write_config(tmp_path, servers: dict) -> None:
    config = load_config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    config.tools.mcp_servers = {name: MCPServerConfig(**cfg) for name, cfg in servers.items()}
    save_config(config)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", path)
    return path


def test_legacy_env_reload_rereads_config_file(tmp_path, config_path):
    _write_config(tmp_path, {"one": {"command": "one"}})
    config = load_config()
    provider = MCPProvider.from_config(
        config, ToolRegistry(), env=LegacyEnvironment.from_config(config)
    )
    assert provider.configured_server_names == {"one"}
    assert set(provider._server_loader()) == {"one"}

    _write_config(tmp_path, {"one": {"command": "one"}, "two": {"command": "two"}})
    assert set(provider._server_loader()) == {"one", "two"}


def test_envless_provider_reads_current_config_file(tmp_path, config_path):
    _write_config(tmp_path, {"one": {"command": "one"}})
    provider = MCPProvider({}, ToolRegistry())
    assert set(provider._server_loader()) == {"one"}


def test_kernel_env_reload_uses_host_source_not_config_file(tmp_path, config_path):
    _write_config(tmp_path, {"ambient": {"command": "ambient"}})
    source = DictConfigSource({"tools": {"mcpServers": {"host": {"command": "host"}}}})
    env = credential_env(root=tmp_path / "host", config=source)
    from nanobot.config.loader import snapshot_config

    provider = MCPProvider.from_config(snapshot_config(source), ToolRegistry(), env=env)
    assert provider.configured_server_names == {"host"}
    assert set(provider._server_loader()) == {"host"}

    source.sections["tools"] = {
        "mcpServers": {"host": {"command": "host"}, "added": {"command": "added"}}
    }
    assert set(provider._server_loader()) == {"host", "added"}


def test_from_config_threads_host_base_env(tmp_path):
    env = credential_env(root=tmp_path / "host", exec_base_env={"COMSPEC": r"D:\host\cmd.exe"})
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    provider = MCPProvider.from_config(config, ToolRegistry(), env=env)
    assert provider._path_kwargs()["base_env"] == {"COMSPEC": r"D:\host\cmd.exe"}


def test_windows_comspec_prefers_host_base_env(monkeypatch):
    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        mcp_mod.shutil, "which", lambda command, path=None: r"C:\nodejs\npx.cmd"
    )
    monkeypatch.setenv("COMSPEC", r"C:\ambient\cmd.exe")

    command, _args, _env = _normalize_windows_stdio_command(
        "npx", [], None, {"COMSPEC": r"D:\host\cmd.exe"}
    )
    assert command == r"D:\host\cmd.exe"

    # Kernel host without COMSPEC: the default, never the process value.
    command, _args, _env = _normalize_windows_stdio_command("npx", [], None, {})
    assert command == "cmd.exe"
