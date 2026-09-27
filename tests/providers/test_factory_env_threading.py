"""Runtime provider rebuilds thread the host env (credentials + data_dir), Task 4.

Closes the Task 3 carry-forward: preset switches, per-run model overrides and the
xAI model catalog used to rebuild providers with ``make_provider(config)`` only,
losing the host data dir and (now) the host credentials.
"""

from __future__ import annotations

from pathlib import Path

from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.providers.factory import build_provider_snapshot, load_provider_snapshot, make_provider
from tests._kernel_env import credential_env

KEY = "sk-or-RESOLVER-ONLY"


def _config(tmp_path: Path) -> Config:
    return Config.model_validate({
        "agents": {"defaults": {
            "workspace": str(tmp_path / "ws"), "model": "openrouter/auto", "provider": "openrouter",
        }},
        "modelPresets": {"alt": {"model": "openrouter/other", "provider": "openrouter"}},
    })


def _env(tmp_path: Path):
    return credential_env(
        {"providers/openrouter/api_key": KEY},
        scopes={"providers/openrouter/api_key": ["provider:openrouter"]},
        root=tmp_path / "host",
    )


def test_build_provider_snapshot_uses_env_credentials(tmp_path):
    snapshot = build_provider_snapshot(_config(tmp_path), env=_env(tmp_path))
    assert snapshot.provider._api_key_for_client == KEY


def test_load_provider_snapshot_accepts_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(_config(tmp_path).model_dump_json(by_alias=True))
    snapshot = load_provider_snapshot(config_path, env=_env(tmp_path))
    assert snapshot.provider._api_key_for_client == KEY


def test_preset_snapshot_loader_threads_env(tmp_path):
    loader = preset_helpers.make_preset_snapshot_loader(_config(tmp_path), None, _env(tmp_path))
    snapshot = loader("alt")
    assert snapshot.model == "openrouter/other"
    assert snapshot.provider._api_key_for_client == KEY


def test_loop_model_override_and_preset_switch_use_host_env(tmp_path, monkeypatch):
    from nanobot.agent import model_runtime

    env = _env(tmp_path)
    config = _config(tmp_path)
    loop = AgentLoop.from_config(config, tool_registry=ToolRegistry(), env=env)
    assert loop.provider._api_key_for_client == KEY
    switched = loop.runtime_resolver.resolve_preset("alt")
    assert switched.provider._api_key_for_client == KEY

    # A per-run model override uses provider "auto", whose matching is config-driven;
    # assert the host env is what the rebuild receives.
    seen = {}
    real = model_runtime.build_provider_snapshot

    def spy(cfg, **kwargs):
        seen.update(kwargs)
        return real(cfg, **{**kwargs, "preset": kwargs["preset"].model_copy(
            update={"provider": "openrouter"})})

    monkeypatch.setattr(model_runtime, "build_provider_snapshot", spy)
    runtime = loop.runtime_resolver.resolve_override(
        model="openrouter/override", model_preset=None, config=config,
    )
    assert seen["env"] is env
    assert runtime is not None
    assert runtime.provider._api_key_for_client == KEY


def test_make_provider_defaults_data_dir_from_env(tmp_path):
    env = _env(tmp_path)
    config = Config.model_validate({"agents": {"defaults": {
        "workspace": str(tmp_path / "ws"), "model": "xai-grok/grok-4.6", "provider": "xai_grok",
    }}})
    provider = make_provider(config, env=env)
    assert provider._data_dir == env.paths.data_dir


async def test_xai_backend_search_catalog_uses_provider_data_dir(tmp_path, monkeypatch):
    from nanobot.providers import xai_grok_provider as xg

    seen: dict[str, object] = {}

    def fake_catalog(proxy=None, *, data_dir=None):
        seen["data_dir"] = data_dir
        raise RuntimeError("stop")

    monkeypatch.setattr(xg, "get_xai_grok_model_catalog", fake_catalog)
    provider = xg.XAIGrokProvider(data_dir=tmp_path / "data")
    try:
        await provider._supports_backend_search("xai-grok/grok-4.6")
    except RuntimeError:
        pass
    assert seen["data_dir"] == tmp_path / "data"


def test_xai_catalog_reads_token_from_data_dir(tmp_path, monkeypatch):
    from nanobot.providers import xai_grok_provider as xg

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        xg, "get_xai_oauth_login_status", lambda *a: calls.append(("status", a)) or None
    )
    monkeypatch.setattr(
        xg, "get_xai_oauth_storage_path", lambda *a: calls.append(("path", a)) or tmp_path / "t"
    )

    def fake_token(**kwargs):
        calls.append(("token", kwargs.get("data_dir")))
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(xg, "get_xai_oauth_token", fake_token)
    xg.invalidate_xai_grok_model_catalog()
    data_dir = tmp_path / "data"
    snapshot = xg.get_xai_grok_model_catalog(None, data_dir=data_dir)
    assert snapshot.source != "remote"
    assert ("status", (data_dir,)) in calls
    assert ("path", (data_dir,)) in calls
    assert ("token", data_dir) in calls
    xg.invalidate_xai_grok_model_catalog()
