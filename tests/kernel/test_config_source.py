"""ConfigSource re-reads (Task 5): the legacy source owns config.json hot reload."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.model_presets import load_model_preset_catalog
from nanobot.config.errors import ConfigLoadError
from nanobot.config.loader import load_config, save_config, snapshot_config
from nanobot.config.schema import Config
from nanobot.core import MoekaCore
from nanobot.kernel.legacy import LegacyEnvironment, file_config_source
from nanobot.providers.factory import load_provider_snapshot
from tests._kernel_env import DictConfigSource, credential_env


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", path)
    return path


def _save(tmp_path, *, model: str, presets: dict | None = None) -> None:
    config = load_config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    config.agents.defaults.model = model
    config.providers.openrouter.api_key = "sk-or-test"
    if presets is not None:
        config.model_presets = presets
    save_config(config)


def test_in_memory_legacy_source_serves_the_held_config(tmp_path):
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    env = LegacyEnvironment.from_config(config)
    assert env.config.snapshot() is config
    assert env.config.section("agents")["defaults"]["workspace"] == str(tmp_path)
    assert env.config.section("model_presets") == {}
    assert env.config.section("nope") == {}


def test_file_backed_legacy_source_rereads_on_change(tmp_path, config_path):
    _save(tmp_path, model="openrouter/first")
    env = LegacyEnvironment.from_config(load_config())
    first = env.config.snapshot()
    assert first.agents.defaults.model == "openrouter/first"
    assert env.config.snapshot() is first  # unchanged bytes: cached

    _save(tmp_path, model="openrouter/second")
    assert env.config.section("agents")["defaults"]["model"] == "openrouter/second"


def test_file_backed_source_propagates_errors_then_recovers(tmp_path, config_path):
    _save(tmp_path, model="openrouter/first")
    env = LegacyEnvironment.from_config(load_config())
    config_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        env.config.snapshot()
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"model": "openrouter/fixed"}}}), encoding="utf-8"
    )
    assert env.config.snapshot().agents.defaults.model == "openrouter/fixed"


def test_building_the_env_reads_no_file(tmp_path, config_path, monkeypatch):
    _save(tmp_path, model="openrouter/first")
    config = load_config()

    def _boom(*_a, **_k):
        raise AssertionError("LegacyEnvironment read config.json at build time")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    LegacyEnvironment.from_config(config)


def test_file_config_source_default_follows_current_config_path(tmp_path, monkeypatch):
    source = file_config_source()
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps({"agents": {"defaults": {"model": "m/a"}}}), encoding="utf-8")
    second.write_text(json.dumps({"agents": {"defaults": {"model": "m/b"}}}), encoding="utf-8")
    monkeypatch.setattr("nanobot.config.loader._current_config_path", first)
    assert snapshot_config(source).agents.defaults.model == "m/a"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", second)
    assert snapshot_config(source).agents.defaults.model == "m/b"


def test_snapshot_config_validates_kernel_sections_without_env_expansion(monkeypatch):
    monkeypatch.setenv("T5_MODEL", "expanded")
    source = DictConfigSource({"agents": {"defaults": {"model": "${T5_MODEL}"}}})
    config = snapshot_config(source)
    assert config.agents.defaults.model == "${T5_MODEL}"
    assert snapshot_config(DictConfigSource()).agents.defaults.model == Config().agents.defaults.model


def test_preset_catalog_loader_reads_env_source(tmp_path, config_path):
    _save(tmp_path, model="openrouter/a", presets={"fast": {"model": "openrouter/fast"}})
    env = LegacyEnvironment.from_config(load_config())
    assert load_model_preset_catalog(env=env)["fast"].model == "openrouter/fast"
    _save(tmp_path, model="openrouter/a", presets={"fast": {"model": "openrouter/faster"}})
    assert load_model_preset_catalog(env=env)["fast"].model == "openrouter/faster"

    kernel = credential_env(
        root=tmp_path / "host",
        config=DictConfigSource({"model_presets": {"host": {"model": "openrouter/host"}}}),
    )
    catalog = load_model_preset_catalog(env=kernel)
    assert set(catalog) == {"host", "default"}


def test_provider_snapshot_loader_reads_env_source(tmp_path, config_path):
    _save(tmp_path, model="openrouter/first")
    env = LegacyEnvironment.from_config(load_config())
    assert load_provider_snapshot(env=env).model == "openrouter/first"
    _save(tmp_path, model="openrouter/second")
    assert load_provider_snapshot(env=env).model == "openrouter/second"
    # An explicit path still reads that file (host-side helper).
    assert load_provider_snapshot(config_path).model == "openrouter/second"


def test_moeka_core_vec_store_lives_in_env_work_dir(tmp_path, monkeypatch):
    seen: list[Path] = []

    def _fake_vec_store(db_path, **_kwargs):
        seen.append(Path(db_path))
        return MagicMock()

    monkeypatch.setattr("nanobot.core.vec_store.VecStore", _fake_vec_store)
    config = Config.model_validate({
        "providers": {"openrouter": {"apiKey": "sk-test-key"}},
        "agents": {"defaults": {
            "model": "openrouter/x",
            "workspace": str(tmp_path / "config-ws"),
            "vec": {"enable": True},
        }},
    })
    env = credential_env(root=tmp_path / "host")
    core = MoekaCore.from_config(config, env=env)
    assert core.loop.workspace == env.paths.work_dir
    assert seen == [env.paths.work_dir / "memory" / "vec.db"]

    seen.clear()
    MoekaCore.from_config(config)
    assert seen == [config.workspace_path / "memory" / "vec.db"]
