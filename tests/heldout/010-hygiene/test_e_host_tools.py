"""Part E: host tools guard (FR-004).

The gateway loop is built as ``nanobot/cli/gateway_runtime.py`` builds it and must carry
``message``, ``cron`` and ``run_cli_app``. A missing host tool is forced in process by
wrapping ``ToolLoader.load`` so it drops named tools after loading (nothing about how the
guard detects the gap is assumed). Each entry point is stopped right after the point where the
guard must already have run by a sentinel (channel manager / API app / first agent turn), so
a tree without the guard fails fast instead of starting a server.
"""

from __future__ import annotations

import json

import pytest
from _h010 import scripted_provider

HOST_TOOLS = ("message", "cron", "run_cli_app")
# Head amendment (2026-10-03): `serve` wires no CronService, so it must not require `cron`.
SERVE_TOOLS = ("message", "run_cli_app")


class Sentinel(Exception):
    """Raised by the patched post-construction step: startup went past the guard."""


@pytest.fixture
def drop_tools(monkeypatch):
    """``drop_tools(*names)``: every ``ToolLoader.load`` afterwards unregisters *names*."""
    from nanobot.agent.tools.loader import ToolLoader

    original = ToolLoader.load

    def install(*names: str) -> None:
        def load(self, ctx, registry, *args, **kwargs):
            result = original(self, ctx, registry, *args, **kwargs)
            for name in names:
                if registry.has(name):
                    registry.unregister(name)
            if isinstance(result, list):
                return [n for n in result if n not in names]
            return result

        monkeypatch.setattr(ToolLoader, "load", load)

    return install


def _exit_code(exc: BaseException) -> int | None:
    code = getattr(exc, "exit_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _text(*parts) -> str:
    return "\n".join(str(p) for p in parts if p)


# -- construction --------------------------------------------------------------------------------


def _gateway_like_loop(tmp_path, *, host_tools=True):
    """``AgentLoop.from_config`` with the gateway's host arguments (fake provider)."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService
    from nanobot.providers.image_generation import image_gen_provider_configs
    from nanobot.triggers.local_store import LocalTriggerStore

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(ws)}}})
    provider, _ = scripted_provider([])
    kwargs = {}
    if host_tools:
        kwargs = dict(
            cron_service=CronService(ws / "cron" / "jobs.json"),
            local_trigger_store=LocalTriggerStore(ws),
            image_generation_provider_configs=image_gen_provider_configs(config),
        )
    return AgentLoop.from_config(
        config, MessageBus(), provider=provider, model="test-model",
        context_window_tokens=32_000, host_tools=host_tools, tool_registry=ToolRegistry(),
        **kwargs,
    )


@pytest.mark.fr("FR-004")
def test_gateway_loop_registers_the_three_host_tools(tmp_path):
    loop = _gateway_like_loop(tmp_path)
    names = set(loop.tools.tool_names)
    for tool in HOST_TOOLS:
        assert tool in names, (tool, sorted(names))


@pytest.mark.fr("FR-004")
def test_kernel_host_without_host_tools_still_constructs(tmp_path):
    loop = _gateway_like_loop(tmp_path, host_tools=False)
    names = set(loop.tools.tool_names)
    assert "read_file" in names
    assert not set(HOST_TOOLS) & names


# -- gateway entry point -------------------------------------------------------------------------


def _run_gateway(tmp_path, monkeypatch):
    import nanobot.channels.manager as channel_manager
    from nanobot.cli.gateway_runtime import _run_gateway as run
    from nanobot.config.schema import Config

    class _StopChannels:
        def __init__(self, *_a, **_k):
            raise Sentinel("gateway reached the channel manager")

    monkeypatch.setattr(channel_manager, "ChannelManager", _StopChannels)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(ws)}},
        "channels": {"websocket": {"enabled": False}},
        "gateway": {"port": 18_997},
    })
    run(config, health_server_enabled=False, webui_static_dist=False,
        webui_bundle_mode="skip", unconfigured_provider_error="h010: no provider configured")


@pytest.mark.fr("FR-004")
def test_gateway_starts_past_the_guard(tmp_path, monkeypatch):
    with pytest.raises(Sentinel):
        _run_gateway(tmp_path, monkeypatch)


@pytest.mark.fr("FR-004")
@pytest.mark.parametrize("missing", [("cron",), ("message", "run_cli_app"), HOST_TOOLS])
def test_gateway_missing_host_tool_exits_2_naming_it(tmp_path, monkeypatch, capsys, logs,
                                                    drop_tools, missing):
    drop_tools(*missing)
    with pytest.raises(BaseException) as info:
        _run_gateway(tmp_path, monkeypatch)
    assert not isinstance(info.value, Sentinel), "gateway started without the host tools"
    assert _exit_code(info.value) == 2, repr(info.value)
    captured = capsys.readouterr()
    text = _text(captured.out, captured.err, info.value, *(m for _, m in logs))
    for name in missing:
        assert name in text, (name, text[-2000:])


# -- serve and agent entry points (in-process CLI) -------------------------------------------------


def _cli_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(ws), "model": "openai/gpt-4o-mini"}},
        "providers": {"openai": {"apiKey": "sk-h010-FAKE-not-a-key"}},
        "api": {"host": "127.0.0.1", "port": 18_996},
    }), encoding="utf-8")
    return cfg, ws


def _result_text(result, logs) -> str:
    try:
        err = result.stderr
    except (ValueError, AttributeError):
        err = ""
    return _text(result.output, err, result.exception, *(m for _, m in logs))


def _invoke(args):
    from typer.testing import CliRunner

    from nanobot.cli.commands import app

    return CliRunner().invoke(app, args)


def _stop_serve(monkeypatch):
    """Stop ``serve`` at ``create_app(agent_loop, ...)``; returns the loop's tool names."""
    import nanobot.api.server as server

    seen: list[str] = []

    def _stop(agent_loop, *_a, **_k):
        seen.extend(agent_loop.tools.tool_names)
        raise Sentinel("serve reached create_app")

    monkeypatch.setattr(server, "create_app", _stop)
    return seen


def _stop_agent(monkeypatch):
    """Stop ``agent -m`` at its first turn; returns the loop's tool names."""
    from nanobot.agent.loop import AgentLoop

    seen: list[str] = []

    async def _stop(self, *_a, **_k):
        seen.extend(self.tools.tool_names)
        raise Sentinel("agent reached its first turn")

    monkeypatch.setattr(AgentLoop, "process_direct", _stop)
    return seen


@pytest.mark.fr("FR-004")
def test_serve_registers_host_tools_and_starts(tmp_path, monkeypatch):
    seen = _stop_serve(monkeypatch)
    cfg, ws = _cli_config(tmp_path)
    result = _invoke(["serve", "--config", str(cfg), "--workspace", str(ws)])
    assert isinstance(result.exception, Sentinel), (result.exit_code, result.output[-2000:])
    for tool in SERVE_TOOLS:
        assert tool in seen, (tool, seen)


@pytest.mark.fr("FR-004")
def test_serve_does_not_require_cron(tmp_path, monkeypatch, drop_tools):
    _stop_serve(monkeypatch)
    drop_tools("cron")
    cfg, ws = _cli_config(tmp_path)
    result = _invoke(["serve", "--config", str(cfg), "--workspace", str(ws)])
    assert isinstance(result.exception, Sentinel), (result.exit_code, result.output[-2000:])


@pytest.mark.fr("FR-004")
@pytest.mark.parametrize("missing", [("run_cli_app",), ("message",), SERVE_TOOLS])
def test_serve_missing_host_tool_exits_2_naming_it(tmp_path, monkeypatch, logs, drop_tools,
                                                  missing):
    _stop_serve(monkeypatch)
    drop_tools(*missing)
    cfg, ws = _cli_config(tmp_path)
    result = _invoke(["serve", "--config", str(cfg), "--workspace", str(ws)])
    assert not isinstance(result.exception, Sentinel), "serve started without the host tools"
    assert result.exit_code == 2, (result.exit_code, repr(result.exception),
                                   result.output[-2000:])
    text = _result_text(result, logs)
    for name in missing:
        assert name in text, (name, text[-2000:])


@pytest.mark.fr("FR-004")
def test_agent_registers_host_tools_and_starts(tmp_path, monkeypatch):
    seen = _stop_agent(monkeypatch)
    cfg, ws = _cli_config(tmp_path)
    result = _invoke(["agent", "-m", "hi", "--config", str(cfg), "--workspace", str(ws)])
    assert isinstance(result.exception, Sentinel), (result.exit_code, result.output[-2000:])
    for tool in HOST_TOOLS:
        assert tool in seen, (tool, seen)


@pytest.mark.fr("FR-004")
@pytest.mark.parametrize("missing", [("message",), ("cron", "run_cli_app")])
def test_agent_missing_host_tool_exits_2_naming_it(tmp_path, monkeypatch, logs, drop_tools,
                                                  missing):
    _stop_agent(monkeypatch)
    drop_tools(*missing)
    cfg, ws = _cli_config(tmp_path)
    result = _invoke(["agent", "-m", "hi", "--config", str(cfg), "--workspace", str(ws)])
    assert not isinstance(result.exception, Sentinel), "agent ran without the host tools"
    assert result.exit_code == 2, (result.exit_code, repr(result.exception),
                                   result.output[-2000:])
    text = _result_text(result, logs)
    for name in missing:
        assert name in text, (name, text[-2000:])
