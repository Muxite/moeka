"""Image generation runtime reload tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.agent import context as agent_context
from nanobot.agent.tools.image_generation import (
    ImageGenerationTool,
    reload_image_generation_tool,
    request_image_generation_reload,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import ToolsConfig


def _runtime_state(tmp_path):
    return SimpleNamespace(
        workspace=tmp_path,
        tools_config=ToolsConfig(),
        _image_generation_provider_configs={},
    )


@pytest.mark.asyncio
async def test_image_generation_reload_replaces_and_removes_live_tool(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    config = load_config()
    config.providers.openrouter.api_key = "first-key"
    config.tools.image_generation.enabled = True
    config.tools.image_generation.provider = "openrouter"
    config.tools.image_generation.model = "openai/first-image-model"
    save_config(config)

    state = _runtime_state(tmp_path)
    registry = ToolRegistry()
    result = await reload_image_generation_tool(state, registry)

    first_tool = registry.get("generate_image")
    assert result["requires_restart"] is False
    assert isinstance(first_tool, ImageGenerationTool)
    assert first_tool.config.model == "openai/first-image-model"
    assert first_tool.provider_configs["openrouter"].api_key == "first-key"

    config = load_config()
    config.providers.openrouter.api_key = "second-key"
    config.tools.image_generation.model = "openai/second-image-model"
    save_config(config)

    result = await reload_image_generation_tool(state, registry)
    second_tool = registry.get("generate_image")
    assert result["requires_restart"] is False
    assert isinstance(second_tool, ImageGenerationTool)
    assert second_tool is not first_tool
    assert second_tool.config.model == "openai/second-image-model"
    assert second_tool.provider_configs["openrouter"].api_key == "second-key"
    assert state.tools_config.image_generation.model == "openai/second-image-model"

    config = load_config()
    config.tools.image_generation.enabled = False
    save_config(config)

    result = await reload_image_generation_tool(state, registry)
    assert result["requires_restart"] is False
    assert not registry.has("generate_image")


@pytest.mark.asyncio
async def test_image_generation_reload_reaches_agent_runtime_control(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    config = load_config()
    config.providers.openrouter.api_key = "image-key"
    config.tools.image_generation.enabled = True
    save_config(config)

    bus = MessageBus()
    state = _runtime_state(tmp_path)
    registry = ToolRegistry()

    async def consume_control() -> None:
        message = await bus.consume_inbound()
        assert await agent_context.handle_runtime_control(state, message, registry) is True

    consumer = asyncio.create_task(consume_control())
    result = await request_image_generation_reload(bus, timeout=2.0)
    await consumer

    assert result["ok"] is True
    assert result["requires_restart"] is False
    assert registry.has("generate_image")


@pytest.mark.asyncio
async def test_image_generation_reload_keeps_host_paths_and_strict_floor(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot reload must not drop the host env's media root / strict floor (Task 3 carry-forward)."""
    from dataclasses import replace

    from tests._kernel_env import DictConfigSource, credential_env

    # A kernel-native host answers from its own ConfigSource, not config.json.
    source = DictConfigSource({"tools": {"imageGeneration": {"enabled": True}}})
    env = replace(credential_env(root=tmp_path / "host", config=source), strict=True)
    state = _runtime_state(tmp_path)
    state.env = env
    registry = ToolRegistry()
    await reload_image_generation_tool(state, registry)

    tool = registry.get("generate_image")
    assert isinstance(tool, ImageGenerationTool)
    assert tool._paths is env.paths
    assert tool._legacy_floor is False


@pytest.mark.asyncio
async def test_image_generation_reload_legacy_env_rereads_config_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy host's env (file-backed Config) hot-reloads from config.json."""
    from nanobot.kernel.legacy import LegacyEnvironment

    config_path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    config = load_config()
    config.agents.defaults.workspace = str(tmp_path)
    config.providers.openrouter.api_key = "first-key"
    config.tools.image_generation.enabled = True
    config.tools.image_generation.model = "openai/first-image-model"
    save_config(config)

    state = _runtime_state(tmp_path)
    state.env = LegacyEnvironment.from_config(load_config())
    registry = ToolRegistry()
    await reload_image_generation_tool(state, registry)
    first = registry.get("generate_image")
    assert isinstance(first, ImageGenerationTool)
    assert first.config.model == "openai/first-image-model"

    config = load_config()
    config.providers.openrouter.api_key = "second-key"
    config.tools.image_generation.model = "openai/second-image-model"
    save_config(config)

    result = await reload_image_generation_tool(state, registry)
    second = registry.get("generate_image")
    assert result["requires_restart"] is False
    assert isinstance(second, ImageGenerationTool)
    assert second.config.model == "openai/second-image-model"
    assert second.provider_configs["openrouter"].api_key == "second-key"
    assert second._paths is state.env.paths

    config_path.write_text("{not json", encoding="utf-8")
    broken = await reload_image_generation_tool(state, registry)
    assert broken["ok"] is False
    assert broken["requires_restart"] is True


@pytest.mark.asyncio
async def test_image_generation_reload_kernel_env_ignores_config_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._kernel_env import DictConfigSource, credential_env

    config_path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    config = load_config()
    config.tools.image_generation.enabled = True
    save_config(config)

    source = DictConfigSource()
    state = _runtime_state(tmp_path)
    state.env = credential_env(root=tmp_path / "host", config=source)
    registry = ToolRegistry()
    await reload_image_generation_tool(state, registry)
    assert not registry.has("generate_image")

    source.sections["tools"] = {"imageGeneration": {"enabled": True, "model": "host/model"}}
    await reload_image_generation_tool(state, registry)
    tool = registry.get("generate_image")
    assert isinstance(tool, ImageGenerationTool)
    assert tool.config.model == "host/model"
