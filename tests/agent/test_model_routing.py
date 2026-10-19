from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nanobot.agent.model_presets import configured_model_presets
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.model_select import ModelSelectTool
from nanobot.config.schema import Config
from nanobot.providers.factory import _resolve_fallback_presets, _resolve_model_preset


def test_daily_model_presets_have_expected_endpoints_and_models() -> None:
    config = Config()
    config.model_routing.enabled = True
    presets = configured_model_presets(config)

    assert presets["luna"].model == "openai/gpt-6-luna"
    assert presets["luna"].provider == "openrouter"
    assert presets["gpu"].model == "gpt-oss:20b"
    assert presets["gpu"].provider == "moeka_gpu"
    assert presets["gpu"].context_window_tokens == 16384
    assert presets["gpu"].max_tokens == 2048
    config.model_routing.gpu_api_base = "http://127.0.0.1:11438/v1"
    assert config.get_api_base(preset=presets["gpu"]) == "http://127.0.0.1:11438/v1"
    assert _resolve_model_preset(config, preset_name="gpu") == presets["gpu"]
    assert [preset.model for preset in _resolve_fallback_presets(config, presets["gpu"])] == [
        "openai/gpt-6-luna"
    ]


@pytest.mark.asyncio
async def test_model_select_lists_readiness_and_selects_session_preset(monkeypatch) -> None:
    presets = {
        "luna": {"model": "openai/gpt-6-luna", "provider": "openrouter"},
        "gpu": {"model": "gpt-oss:20b", "provider": "moeka_gpu"},
    }
    control = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            model_presets=presets,
            model="openai/gpt-6-luna",
            model_preset="luna",
        ),
        set_model_preset=Mock(return_value=SimpleNamespace(model="gpt-oss:20b")),
        resolve_model_preset=Mock(
            return_value=SimpleNamespace(
                model="gpt-oss:20b",
                provider=SimpleNamespace(provider_name="openrouter"),
            )
        ),
    )
    tool = ModelSelectTool(control)
    ready = AsyncMock(return_value=True)
    monkeypatch.setattr("nanobot.agent.tools.model_select._gpu_ready", ready)

    output = await tool.execute(choice="status")
    assert "GPU (gpu, gpt-oss:20b): ready" in str(output)

    token = bind_request_context(RequestContext(channel="telegram", chat_id="42", session_key="s1"))
    try:
        output = await tool.execute(choice="gpu")
    finally:
        reset_request_context(token)
    assert "next model call" in str(output)
    assert [call.args[0] for call in control.resolve_model_preset.call_args_list] == [
        "luna",
        "gpu",
    ]
    control.set_model_preset.assert_not_called()
    assert ready.await_count == 2


@pytest.mark.asyncio
async def test_gpu_selection_still_selects_when_probe_fails(monkeypatch) -> None:
    control = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            model_presets={
                "luna": {"model": "openai/gpt-6-luna", "provider": "openrouter"},
                "gpu": {"model": "gpt-oss:20b", "provider": "moeka_gpu"},
            },
            model="openai/gpt-6-luna",
            model_preset="luna",
        ),
        set_model_preset=lambda name, *, session_key: SimpleNamespace(model="gpt-oss:20b"),
        resolve_model_preset=lambda name: SimpleNamespace(
            model="gpt-oss:20b",
            provider=SimpleNamespace(provider_name="openrouter")
        ),
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.model_select._gpu_ready",
        AsyncMock(return_value=False),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="42", session_key="s1"))
    try:
        output = await ModelSelectTool(control).execute(choice="gpu")
    finally:
        reset_request_context(token)
    assert "bounded attempt will fall back to Luna" in str(output)


@pytest.mark.asyncio
async def test_explicit_session_model_selection_blocks_automatic_switch() -> None:
    control = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            model_presets={"luna": {}, "gpu": {}},
            model="openai/gpt-6-luna",
            model_preset="luna",
        ),
        resolve_model_preset=Mock(),
    )
    token = bind_request_context(
        RequestContext(
            channel="telegram",
            chat_id="42",
            session_key="s1",
            attributes={"_user_model_lock": True},
        )
    )
    try:
        output = await ModelSelectTool(control).execute(choice="gpu")
    finally:
        reset_request_context(token)
    assert "explicitly selected" in str(output)
    control.resolve_model_preset.assert_not_called()
