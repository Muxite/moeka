"""Integration tests for config loading — real files and env vars, no mocks.

Tests load_config / save_config / resolve_config_env_vars against actual
temp files and process environment variables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nanobot.config.loader import (
    load_config,
    resolve_config_env_vars,
    save_config,
)
from nanobot.config.schema import Config


# ---------------------------------------------------------------------------
# load_config — real JSON files
# ---------------------------------------------------------------------------

class TestLoadConfigReal:

    def test_loads_empty_file_as_defaults(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        config = load_config(cfg_file)
        assert isinstance(config, Config)
        assert config.agents.defaults.model  # non-empty default model

    def test_loads_custom_model(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"agents": {"defaults": {"model": "openai/gpt-4o"}}}),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        assert config.agents.defaults.model == "openai/gpt-4o"

    def test_loads_provider_api_key(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"providers": {"openrouter": {"apiKey": "sk-test-key"}}}),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        assert config.providers.openrouter.api_key == "sk-test-key"

    def test_returns_defaults_when_file_missing(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.json")
        assert isinstance(config, Config)

    def test_returns_defaults_on_malformed_json(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "bad.json"
        cfg_file.write_text("{ this is not json }", encoding="utf-8")
        config = load_config(cfg_file)
        assert isinstance(config, Config)

    def test_loads_channels_enabled_flag(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"channels": {"telegram": {"enabled": True, "token": "tok"}}}),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        section = getattr(config.channels, "telegram", None)
        if isinstance(section, dict):
            assert section["enabled"] is True
        else:
            assert getattr(section, "enabled", None) is True

    def test_loads_tools_exec_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"tools": {"exec": {"timeout": 120}}}),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        assert config.tools.exec.timeout == 120


# ---------------------------------------------------------------------------
# save_config — real atomic writes
# ---------------------------------------------------------------------------

class TestSaveConfigReal:

    def test_save_creates_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        config = Config()
        save_config(config, cfg_file)
        assert cfg_file.exists()

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        config = Config()
        config.agents.defaults.model = "anthropic/claude-opus-4-7"
        save_config(config, cfg_file)

        loaded = load_config(cfg_file)
        assert loaded.agents.defaults.model == "anthropic/claude-opus-4-7"

    def test_save_is_valid_json(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        save_config(Config(), cfg_file)
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "nested" / "sub" / "config.json"
        save_config(Config(), cfg_file)
        assert cfg_file.exists()

    def test_save_atomic_leaves_no_tmp_on_success(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        save_config(Config(), cfg_file)
        # Atomic write uses .json.tmp — must be gone after success
        assert not (tmp_path / "config.json.tmp").exists()

    def test_save_preserves_extra_channel_config(self, tmp_path: Path) -> None:
        """Channel sections with extra keys survive a save/load cycle."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({
                "channels": {
                    "telegram": {"enabled": True, "token": "tok", "allowFrom": ["*"]}
                }
            }),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        save_config(config, cfg_file)
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["channels"]["telegram"]["token"] == "tok"


# ---------------------------------------------------------------------------
# resolve_config_env_vars — real environment
# ---------------------------------------------------------------------------

class TestResolveEnvVarsReal:

    def test_resolves_real_env_var(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("INTEGRATION_TEST_TOKEN", "real_token_value")
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"providers": {"groq": {"apiKey": "${INTEGRATION_TEST_TOKEN}"}}}),
            encoding="utf-8",
        )
        raw = load_config(cfg_file)
        resolved = resolve_config_env_vars(raw)
        assert resolved.providers.groq.api_key == "real_token_value"

    def test_unresolved_placeholder_kept_as_is(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("UNSET_INTEGRATION_VAR_XYZ", raising=False)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"providers": {"groq": {"apiKey": "${UNSET_INTEGRATION_VAR_XYZ}"}}}),
            encoding="utf-8",
        )
        raw = load_config(cfg_file)
        resolved = resolve_config_env_vars(raw)
        # Unresolved var → placeholder left in place (warning logged, no crash)
        assert "${UNSET_INTEGRATION_VAR_XYZ}" in (resolved.providers.groq.api_key or "")

    def test_workspace_placeholder_fallback_when_env_not_set(self, monkeypatch) -> None:
        """workspace_path must never return a path with ${...} in it."""
        monkeypatch.delenv("MOEKA_WORKSPACE", raising=False)
        monkeypatch.delenv("MOEKA_STATE", raising=False)
        monkeypatch.delenv("NANOBOT_HOME", raising=False)
        config = Config()
        config.agents.defaults.workspace = "${MOEKA_WORKSPACE}"
        path = config.workspace_path
        assert "${" not in str(path), (
            f"workspace_path must not contain unexpanded placeholder, got: {path}"
        )
        # Must resolve to the default state home (~/.nanobot)
        assert path == Path.home() / ".nanobot"

    def test_workspace_placeholder_resolved_when_env_set(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MOEKA_WORKSPACE", str(tmp_path / "mybot"))
        config = Config()
        config.agents.defaults.workspace = "${MOEKA_WORKSPACE}"
        path = config.workspace_path
        assert "${" not in str(path)
        assert path == tmp_path / "mybot"


# ---------------------------------------------------------------------------
# workspace_path correctness
# ---------------------------------------------------------------------------

class TestWorkspacePathReal:

    def test_tilde_path_expands(self) -> None:
        config = Config()
        config.agents.defaults.workspace = "~/.nanobot"
        assert config.workspace_path == Path.home() / ".nanobot"

    def test_absolute_path_unchanged(self, tmp_path: Path) -> None:
        config = Config()
        config.agents.defaults.workspace = str(tmp_path / "bot")
        assert config.workspace_path == tmp_path / "bot"

    def test_default_workspace_resolves_without_env(self, monkeypatch) -> None:
        monkeypatch.delenv("MOEKA_WORKSPACE", raising=False)
        monkeypatch.delenv("MOEKA_STATE", raising=False)
        monkeypatch.delenv("NANOBOT_HOME", raising=False)
        config = Config()
        # Default workspace string is "~/.nanobot"
        assert config.workspace_path == Path.home() / ".nanobot"
