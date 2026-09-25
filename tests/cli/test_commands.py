import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from nanobot.bus.events import OutboundMessage
from nanobot.cli import provider as provider_commands
from nanobot.cli import terminal as cli_terminal
from nanobot.cli.commands import app
from nanobot.config.schema import Config
from nanobot.providers.factory import make_provider, provider_signature
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_name

runner = CliRunner()


def _fake_provider():
    """Return a minimal fake provider that satisfies AgentLoop.__init__."""
    p = MagicMock()
    p.generation.max_tokens = 4096
    return p


def test_interactive_tty_mode_restores_line_input(monkeypatch) -> None:
    try:
        import os
        import pty
        import termios
    except ImportError:  # pragma: no cover - platform without POSIX termios
        pytest.skip("termios unavailable")

    master_fd, slave_fd = pty.openpty()

    class _Stdin:
        def fileno(self) -> int:
            return slave_fd

    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[0] &= ~termios.ICRNL
        attrs[0] |= termios.IGNCR
        attrs[3] &= ~(termios.ISIG | termios.ICANON | termios.ECHO)
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        monkeypatch.setattr(cli_terminal.sys, "stdin", _Stdin())
        cli_terminal._ensure_interactive_tty_mode()

        restored = termios.tcgetattr(slave_fd)
        assert restored[0] & termios.ICRNL
        assert not restored[0] & termios.IGNCR
        assert restored[3] & termios.ISIG
        assert restored[3] & termios.ICANON
        assert restored[3] & termios.ECHO
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
    return ansi_escape.sub('', text)


def test_status_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output


def test_status_uses_explicit_config_and_workspace(tmp_path: Path):
    config_path = tmp_path / "instance" / "config.json"
    config_workspace = tmp_path / "config-workspace"
    override_workspace = tmp_path / "override-workspace"
    config = Config()
    config.agents.defaults.workspace = str(config_workspace)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config.model_dump(mode="json", by_alias=True)))

    result = runner.invoke(
        app,
        ["status", "--config", str(config_path), "--workspace", str(override_workspace)],
    )

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    compact_output = stripped_output.replace("\n", "")
    assert str(config_path.resolve(strict=False)) in compact_output
    assert str(override_workspace) in compact_output
    assert str(config_workspace) not in compact_output

def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.6-sol"

    assert config.get_provider_name() == "openai_codex"


def test_openai_codex_oauth_default_matches_curated_flagship():
    spec = find_by_name("openai_codex")

    assert spec is not None
    assert spec.builtin_models
    assert provider_commands._OAUTH_PROVIDER_DEFAULT_MODELS["openai_codex"] == (
        spec.builtin_models[0].id
    )


def test_config_dump_excludes_oauth_provider_blocks():
    config = Config()

    providers = config.model_dump(by_alias=True)["providers"]

    assert "openaiCodex" not in providers
    assert "xaiGrok" not in providers
    assert "githubCopilot" not in providers


def test_provider_logout_openai_codex_removes_local_oauth_files(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "codex.json"
    lock_path = token_path.with_suffix(".lock")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("{}", encoding="utf-8")
    lock_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_path))

    result = runner.invoke(app, ["provider", "logout", "openai-codex"])

    assert result.exit_code == 0
    assert not token_path.exists()
    assert not lock_path.exists()
    assert "Logged out from OpenAI Codex" in result.stdout


def test_provider_logout_openai_codex_succeeds_when_no_local_oauth_file(monkeypatch, tmp_path):
    token_path = tmp_path / "auth" / "codex.json"
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_path))

    result = runner.invoke(app, ["provider", "logout", "openai-codex"])

    assert result.exit_code == 0
    assert "No local OAuth credentials found for OpenAI Codex" in result.stdout


def test_provider_logout_xai_grok_removes_instance_credentials(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "xai.json"
    lock_path = token_path.with_suffix(".lock")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("{}", encoding="utf-8")
    lock_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "nanobot.providers.xai_oauth.get_xai_oauth_storage_path",
        lambda: token_path,
    )

    result = runner.invoke(app, ["provider", "logout", "xai-grok"])

    assert result.exit_code == 0
    assert not token_path.exists()
    assert "Logged out from xAI Grok" in result.stdout


def test_provider_logout_xai_grok_uses_explicit_config_path(tmp_path, monkeypatch):
    from nanobot.config import loader

    default_config = tmp_path / "default" / "config.json"
    selected_config = tmp_path / "selected" / "config.json"
    default_token = default_config.parent / "auth" / "xai.json"
    selected_token = selected_config.parent / "auth" / "xai.json"
    for token_path in (default_token, selected_token):
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(loader, "_current_config_path", default_config)

    result = runner.invoke(
        app,
        ["provider", "logout", "xai-grok", "--config", str(selected_config)],
    )

    assert result.exit_code == 0
    assert default_token.exists()
    assert not selected_token.exists()
    assert "Using config:" in result.stdout


def test_provider_logout_github_copilot_removes_local_oauth_files(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "github-copilot.json"
    lock_path = token_path.with_suffix(".lock")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("{}", encoding="utf-8")
    lock_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_path))

    result = runner.invoke(app, ["provider", "logout", "github-copilot"])

    assert result.exit_code == 0
    assert not token_path.exists()
    assert not lock_path.exists()
    assert "Logged out from GitHub Copilot" in result.stdout


def test_provider_logout_github_copilot_succeeds_when_no_local_oauth_file(monkeypatch, tmp_path):
    token_path = tmp_path / "auth" / "github-copilot.json"
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_path))

    result = runner.invoke(app, ["provider", "logout", "github-copilot"])

    assert result.exit_code == 0
    assert "No local OAuth credentials found for GitHub Copilot" in result.stdout


def test_provider_logout_rejects_unknown_provider():
    result = runner.invoke(app, ["provider", "logout", "not-a-real-provider"])

    assert result.exit_code == 1
    assert "Unknown OAuth provider" in result.stdout


def test_provider_logout_paths_resolve_to_expected_files():
    from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
    from oauth_cli_kit.storage import FileTokenStorage

    from nanobot.providers.github_copilot_provider import get_storage
    from nanobot.providers.xai_oauth import get_xai_oauth_storage_path

    codex_storage = FileTokenStorage(token_filename=OPENAI_CODEX_PROVIDER.token_filename)
    codex_path = codex_storage.get_token_path()
    assert codex_path.name == "codex.json"
    assert codex_path.parent.name == "auth"

    xai_path = get_xai_oauth_storage_path()
    assert xai_path.name == "xai.json"
    assert xai_path.parent.name == "auth"

    gh_storage = get_storage()
    gh_path = gh_storage.get_token_path()
    assert gh_path.name == "github-copilot.json"
    assert gh_path.parent.name == "auth"


def test_provider_login_rejects_unknown_provider():
    result = runner.invoke(app, ["provider", "login", "not-a-real-provider"])

    assert result.exit_code == 1
    assert "Unknown OAuth provider" in result.stdout


def test_provider_login_openai_codex_handles_missing_oauth_symbol(monkeypatch):
    import oauth_cli_kit

    monkeypatch.delattr(oauth_cli_kit, "get_token")

    result = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert result.exit_code == 1
    assert (
        "This nanobot installation is missing the required oauth-cli-kit package. "
        "Reinstall or upgrade nanobot-ai using the same installation method."
    ) in re.sub(r"\s+", " ", result.stdout)
    assert result.exception is not None


def test_provider_login_can_set_openai_codex_as_main_provider(tmp_path):
    config_path = tmp_path / "config.json"
    called = False
    original = provider_commands._LOGIN_HANDLERS["openai_codex"]

    def fake_login() -> None:
        nonlocal called
        called = True

    provider_commands._LOGIN_HANDLERS["openai_codex"] = fake_login
    try:
        result = runner.invoke(
            app,
            [
                "provider",
                "login",
                "openai-codex",
                "--set-main",
                "--config",
                str(config_path),
            ],
        )
    finally:
        provider_commands._LOGIN_HANDLERS["openai_codex"] = original

    assert result.exit_code == 0
    assert called is True
    assert "Set openai-codex as the main provider" in result.stdout

    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.agents.defaults.provider == "openai_codex"
    assert saved.agents.defaults.model == "openai-codex/gpt-5.6-sol"
    assert saved.agents.defaults.model_preset is None
    assert make_provider(saved).__class__.__name__ == "OpenAICodexProvider"


def test_provider_login_can_set_github_copilot_as_main_provider(tmp_path):
    config_path = tmp_path / "config.json"
    original = provider_commands._LOGIN_HANDLERS["github_copilot"]
    provider_commands._LOGIN_HANDLERS["github_copilot"] = lambda: None
    try:
        result = runner.invoke(
            app,
            [
                "provider",
                "login",
                "github-copilot",
                "--set-main",
                "--config",
                str(config_path),
            ],
        )
    finally:
        provider_commands._LOGIN_HANDLERS["github_copilot"] = original

    assert result.exit_code == 0
    assert "Set github-copilot as the main provider" in result.stdout

    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.agents.defaults.provider == "github_copilot"
    assert saved.agents.defaults.model == "github-copilot/gpt-5.4-mini"
    assert saved.agents.defaults.model_preset is None
    assert make_provider(saved).__class__.__name__ == "GitHubCopilotProvider"


def test_provider_login_can_set_xai_grok_as_main_provider(tmp_path):
    config_path = tmp_path / "config.json"
    original = provider_commands._LOGIN_HANDLERS["xai_grok"]
    provider_commands._LOGIN_HANDLERS["xai_grok"] = lambda: None
    try:
        result = runner.invoke(
            app,
            [
                "provider",
                "login",
                "xai-grok",
                "--set-main",
                "--config",
                str(config_path),
            ],
        )
    finally:
        provider_commands._LOGIN_HANDLERS["xai_grok"] = original

    assert result.exit_code == 0
    assert "Set xai-grok as the main provider" in result.stdout

    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.agents.defaults.provider == "xai_grok"
    assert saved.agents.defaults.model == "xai-grok/grok-4.6"
    assert saved.agents.defaults.context_window_tokens == 500_000
    assert saved.agents.defaults.model_preset is None
    assert make_provider(saved).__class__.__name__ == "XAIGrokProvider"


def test_provider_login_model_implies_set_main_provider(tmp_path):
    config_path = tmp_path / "config.json"
    original = provider_commands._LOGIN_HANDLERS["github_copilot"]
    provider_commands._LOGIN_HANDLERS["github_copilot"] = lambda: None
    try:
        result = runner.invoke(
            app,
            [
                "provider",
                "login",
                "github-copilot",
                "--model",
                "github-copilot/gpt-5.4-mini",
                "--config",
                str(config_path),
            ],
        )
    finally:
        provider_commands._LOGIN_HANDLERS["github_copilot"] = original

    assert result.exit_code == 0
    assert "Set github-copilot as the main provider" in result.stdout

    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.agents.defaults.provider == "github_copilot"
    assert saved.agents.defaults.model == "github-copilot/gpt-5.4-mini"
    assert make_provider(saved).__class__.__name__ == "GitHubCopilotProvider"


def test_provider_login_openai_codex_passes_configured_proxy(monkeypatch):
    proxy = "http://127.0.0.1:23458"
    monkeypatch.setattr(
        "nanobot.config.loader.load_config",
        lambda: Config.model_validate({"providers": {"openaiCodex": {"proxy": proxy}}}),
    )

    import oauth_cli_kit

    def fake_get_token(**_kwargs):
        raise RuntimeError("no-token")

    monkeypatch.setattr(oauth_cli_kit, "get_token", fake_get_token)

    captured: dict[str, str | None] = {}

    def fake_login(*, print_fn, prompt_fn, proxy=None):
        captured["proxy"] = proxy
        return SimpleNamespace(access="access-token", account_id="acct-test")

    monkeypatch.setattr(oauth_cli_kit, "login_oauth_interactive", fake_login)

    result = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert result.exit_code == 0
    assert captured["proxy"] == proxy


def test_provider_login_openai_codex_uses_explicit_config_proxy(tmp_path, monkeypatch):
    from nanobot.config import loader

    proxy = "http://127.0.0.1:23458"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"providers": {"openaiCodex": {"proxy": proxy}}}),
        encoding="utf-8",
    )
    active_path: dict[str, Path] = {}
    real_load_config = loader.load_config

    def fake_set_config_path(path: Path) -> None:
        active_path["path"] = path

    def fake_load_config(config_path: Path | None = None) -> Config:
        path = config_path or active_path.get("path")
        if path is None:
            return Config.model_validate(
                {"providers": {"openaiCodex": {"proxy": "http://default-proxy:8080"}}}
            )
        return real_load_config(path)

    monkeypatch.setattr(loader, "set_config_path", fake_set_config_path)
    monkeypatch.setattr(loader, "load_config", fake_load_config)

    import oauth_cli_kit

    captured: dict[str, str | None] = {}

    def fake_get_token(*, proxy=None):
        captured["proxy"] = proxy
        return SimpleNamespace(access="access-token", account_id="acct-test")

    monkeypatch.setattr(oauth_cli_kit, "get_token", fake_get_token)

    result = runner.invoke(
        app,
        ["provider", "login", "openai-codex", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert active_path["path"] == config_path.resolve()
    assert captured["proxy"] == proxy


def test_provider_login_openai_codex_resolves_proxy_env_ref(monkeypatch):
    proxy = "http://127.0.0.1:23458"
    monkeypatch.setenv("CODEX_PROXY_FOR_TEST", proxy)
    monkeypatch.setattr(
        "nanobot.config.loader.load_config",
        lambda: Config.model_validate(
            {"providers": {"openaiCodex": {"proxy": "${CODEX_PROXY_FOR_TEST}"}}}
        ),
    )

    import oauth_cli_kit

    captured: dict[str, str | None] = {}

    def fake_get_token(*, proxy=None):
        captured["proxy"] = proxy
        return SimpleNamespace(access="access-token", account_id="acct-test")

    monkeypatch.setattr(oauth_cli_kit, "get_token", fake_get_token)

    result = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert result.exit_code == 0
    assert captured["proxy"] == proxy


def test_provider_login_xai_grok_runs_browser_flow_with_configured_proxy(monkeypatch):
    proxy = "http://127.0.0.1:23458"
    monkeypatch.setattr(
        "nanobot.config.loader.load_config",
        lambda: Config.model_validate({"providers": {"xaiGrok": {"proxy": proxy}}}),
    )
    monkeypatch.setattr(
        "nanobot.providers.xai_oauth.get_xai_oauth_token",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("not signed in")),
    )
    captured: dict[str, object] = {}

    def fake_login(*, print_fn, prompt_fn, proxy=None):
        captured.update(print_fn=print_fn, prompt_fn=prompt_fn, proxy=proxy)
        return SimpleNamespace(access="access-token", account_id="user@example.com")

    monkeypatch.setattr("nanobot.providers.xai_oauth.login_xai_oauth", fake_login)

    result = runner.invoke(app, ["provider", "login", "xai-grok"])

    assert result.exit_code == 0
    assert captured["proxy"] == proxy
    assert callable(captured["print_fn"])
    assert callable(captured["prompt_fn"])
    assert "Hosted X Search is enabled automatically when the selected model supports it" in result.stdout


def test_config_matches_explicit_ollama_prefix_without_api_key():
    config = Config()
    config.agents.defaults.model = "ollama/llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434/v1"


def test_config_explicit_ollama_provider_uses_default_localhost_api_base():
    config = Config()
    config.agents.defaults.provider = "ollama"
    config.agents.defaults.model = "llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434/v1"


def test_config_accepts_camel_case_explicit_provider_name_for_coding_plan():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "volcengineCodingPlan",
                    "model": "doubao-1-5-pro",
                }
            },
            "providers": {
                "volcengineCodingPlan": {
                    "apiKey": "test-key",
                }
            },
        }
    )

    assert config.get_provider_name() == "volcengine_coding_plan"
    assert config.get_api_base() == "https://ark.cn-beijing.volces.com/api/coding/v3"


def test_config_accepts_lm_studio_without_api_key_and_uses_default_localhost_api_base():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "lm_studio",
                    "model": "local-model",
                }
            },
            "providers": {
                "lmStudio": {
                    "apiKey": None,
                }
            },
        }
    )

    assert config.get_provider_name() == "lm_studio"
    assert config.get_api_key() is None
    assert config.get_api_base() == "http://localhost:1234/v1"


def test_config_accepts_atomic_chat_without_api_key_and_uses_default_localhost_api_base():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "atomic_chat",
                    "model": "local-model",
                }
            },
            "providers": {
                "atomicChat": {
                    "apiKey": None,
                }
            },
        }
    )

    assert config.get_provider_name() == "atomic_chat"
    assert config.get_api_key() is None
    assert config.get_api_base() == "http://localhost:1337/v1"


def test_find_by_name_accepts_camel_case_and_hyphen_aliases():
    assert find_by_name("volcengineCodingPlan") is not None
    assert find_by_name("volcengineCodingPlan").name == "volcengine_coding_plan"
    assert find_by_name("github-copilot") is not None
    assert find_by_name("github-copilot").name == "github_copilot"
    assert find_by_name("longcat") is not None
    assert find_by_name("longcat").name == "longcat"
    assert find_by_name("atomic-chat") is not None
    assert find_by_name("atomic-chat").name == "atomic_chat"


def test_config_explicit_longcat_provider_resolves_provider_name():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "longcat",
                    "model": "LongCat-Flash-Chat",
                }
            },
            "providers": {
                "longcat": {
                    "apiKey": "test-key",
                }
            },
        }
    )

    assert config.get_provider_name() == "longcat"
    assert config.get_api_base() == "https://api.longcat.chat/openai/v1"


def test_config_auto_detects_longcat_from_model_keyword():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "longcat/LongCat-Flash-Chat"}},
            "providers": {"longcat": {"apiKey": "test-key"}},
        }
    )

    assert config.get_provider_name() == "longcat"


def test_config_explicit_xiaomi_mimo_provider_uses_default_api_base():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "xiaomi_mimo",
                    "model": "MiniMax-M1-80k",
                }
            },
            "providers": {
                "xiaomiMimo": {
                    "apiKey": "test-key",
                }
            },
        }
    )

    assert config.get_provider_name() == "xiaomi_mimo"
    assert config.get_api_base() == "https://api.xiaomimimo.com/v1"


def test_config_auto_detects_xiaomi_mimo_from_model_keyword():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "mimo/MiniMax-M1-80k"}},
            "providers": {"xiaomiMimo": {"apiKey": "test-key"}},
        }
    )

    assert config.get_provider_name() == "xiaomi_mimo"
    assert config.get_api_base() == "https://api.xiaomimimo.com/v1"


def test_config_explicit_minimax_anthropic_provider_uses_default_api_base():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "minimax_anthropic",
                    "model": "MiniMax-M2.7-highspeed",
                }
            },
            "providers": {
                "minimaxAnthropic": {
                    "apiKey": "test-key",
                }
            },
        }
    )

    assert config.get_provider_name() == "minimax_anthropic"
    assert config.get_api_key() == "test-key"
    assert config.get_api_base() == "https://api.minimax.io/anthropic"


def test_config_auto_detects_ollama_from_local_api_base():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434/v1"}},
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434/v1"


def test_config_prefers_ollama_over_vllm_when_both_local_providers_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
                "ollama": {"apiBase": "http://localhost:11434/v1"},
            },
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434/v1"


def test_config_falls_back_to_vllm_when_ollama_not_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
            },
        }
    )

    assert config.get_provider_name() == "vllm"
    assert config.get_api_base() == "http://localhost:8000"


def test_config_cloud_nemotron_is_not_hijacked_by_unconfigured_ollama():
    """`nvidia/nemotron-*` via a gateway must not route to Ollama when no
    Ollama endpoint is configured. Ollama keeps "nemotron" in its keywords
    for bare-model auto-routing (PR #1863), which previously hijacked
    cloud-hosted nemotron variants and silently sent traffic to
    http://localhost:11434/v1."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "nvidia/nemotron-3-super-120b-a12b",
                }
            },
            "providers": {"openrouter": {"apiKey": "sk-or-test"}},
        }
    )

    assert config.get_provider_name() == "openrouter"
    assert config.get_api_base() == "https://openrouter.ai/api/v1"


def test_config_bare_nemotron_still_auto_routes_to_configured_ollama():
    """Preserves PR #1863 intent: when the user has actually configured an
    Ollama endpoint, a bare nemotron model still auto-routes there."""
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "nemotron-3-nano"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434/v1"}},
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434/v1"


def test_config_cloud_nemotron_is_not_hijacked_by_configured_ollama():
    """An explicit cloud namespace takes precedence over local keywords."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "nvidia/nemotron-3-super-120b-a12b",
                }
            },
            "providers": {
                "ollama": {"apiBase": "http://localhost:11434/v1"},
                "openrouter": {"apiKey": "sk-or-test"},
            },
        }
    )

    assert config.get_provider_name() == "openrouter"
    assert config.get_api_base() == "https://openrouter.ai/api/v1"


def test_openai_compat_provider_passes_model_through():
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(default_model="github-copilot/gpt-5.3-codex")

    assert provider.get_default_model() == "github-copilot/gpt-5.3-codex"


def test_openai_codex_proxy_config_affects_provider_and_signature():
    def config_with_proxy(proxy: str) -> Config:
        return Config.model_validate(
            {
                "agents": {
                    "defaults": {
                        "provider": "openai-codex",
                        "model": "openai-codex/gpt-5.5",
                    }
                },
                "providers": {"openaiCodex": {"proxy": proxy}},
            }
        )

    proxy = "http://127.0.0.1:23458"
    config = config_with_proxy(proxy)

    provider = make_provider(config)

    assert provider.__class__.__name__ == "OpenAICodexProvider"
    assert provider.proxy == proxy
    assert provider_signature(config) != provider_signature(
        config_with_proxy("http://127.0.0.1:23459")
    )


def test_provider_proxy_rejects_unsupported_backend():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "anthropic",
                    "model": "anthropic/claude-opus-4-5",
                }
            },
            "providers": {
                "anthropic": {
                    "apiKey": "sk-test",
                    "proxy": "http://127.0.0.1:23458",
                }
            },
        }
    )

    with pytest.raises(ValueError, match=r"providers\.anthropic\.proxy"):
        make_provider(config)


def test_github_copilot_provider_strips_prefixed_model_name():
    from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = GitHubCopilotProvider(default_model="github-copilot/gpt-5.1")

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="github-copilot/gpt-5.1",
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "gpt-5.1"


@pytest.mark.asyncio
async def test_github_copilot_provider_refreshes_client_api_key_before_chat():
    from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

    mock_client = MagicMock()
    mock_client.api_key = "no-key"
    mock_client.chat.completions.create = AsyncMock(return_value={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI", return_value=mock_client):
        provider = GitHubCopilotProvider(default_model="github-copilot/gpt-4")
        await provider._ensure_client()

    provider._get_copilot_access_token = AsyncMock(return_value="copilot-access-token")

    response = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="github-copilot/gpt-4",
        max_tokens=16,
        temperature=0.1,
    )

    assert response.content == "ok"
    assert provider._client.api_key == "copilot-access-token"
    provider._get_copilot_access_token.assert_awaited_once()
    mock_client.chat.completions.create.assert_awaited_once()


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.6-sol") == "gpt-5.6-sol"
    assert _strip_model_prefix("openai_codex/gpt-5.6-sol") == "gpt-5.6-sol"


def test_make_provider_passes_extra_headers_to_custom_provider():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "gpt-4o-mini"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "https://example.com/v1",
                    "extraHeaders": {
                        "APP-Code": "demo-app",
                        "x-session-affinity": "sticky-session",
                    },
                }
            }
        }
    )

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = make_provider(config)
        asyncio.run(provider._ensure_client())

    kwargs = mock_async_openai.call_args.kwargs
    assert kwargs["api_key"] == "test-key"
    assert kwargs["base_url"] == "https://example.com/v1"
    assert kwargs["default_headers"]["APP-Code"] == "demo-app"
    assert kwargs["default_headers"]["x-session-affinity"] == "sticky-session"


def test_make_provider_treats_dynamic_custom_provider_as_direct():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "my-company-api", "model": "gpt-4o-mini"}},
            "providers": {
                "my-company-api": {
                    "apiBase": "https://example.com/v1",
                }
            },
        }
    )

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = make_provider(config)
        asyncio.run(provider._ensure_client())

    assert provider.get_default_model() == "gpt-4o-mini"
    assert provider._spec.name == "my_company_api"
    assert provider._spec.is_direct is True
    kwargs = mock_async_openai.call_args.kwargs
    assert kwargs["api_key"] == "no-key"
    assert kwargs["base_url"] == "https://example.com/v1"


def test_make_provider_strips_dynamic_custom_route_prefix_from_request_model():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "my-company-api/gpt-4o-mini"}},
            "providers": {
                "my-company-api": {
                    "apiBase": "https://example.com/v1",
                }
            },
        }
    )

    provider = make_provider(config)

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )
    body = provider._build_responses_body(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert config.get_provider_name() == "my-company-api"
    assert kwargs["model"] == "gpt-4o-mini"
    assert body["model"] == "gpt-4o-mini"


def test_make_provider_preserves_namespaced_model_for_forced_dynamic_provider():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "my-company-api",
                    "model": "openai/gpt-4o-mini",
                }
            },
            "providers": {
                "my-company-api": {
                    "apiBase": "https://example.com/v1",
                }
            },
        }
    )

    provider = make_provider(config)
    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "openai/gpt-4o-mini"


def test_make_provider_strips_dynamic_custom_route_prefix_once():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "my-company-api/openai/gpt-4o-mini",
                }
            },
            "providers": {
                "my-company-api": {
                    "apiBase": "https://example.com/v1",
                }
            },
        }
    )

    provider = make_provider(config)
    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "openai/gpt-4o-mini"


def test_make_provider_rejects_dynamic_custom_provider_without_api_base():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "my-company-api", "model": "gpt-4o-mini"}},
            "providers": {
                "my-company-api": {
                    "apiKey": "sk-test",
                }
            },
        }
    )

    with pytest.raises(ValueError, match="Provider 'my-company-api' requires api_base"):
        make_provider(config)


def test_make_provider_rejects_auto_dynamic_custom_prefix_without_api_base():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "companyProxy/gpt-4o"}},
            "providers": {
                "otherProxy": {
                    "apiBase": "https://other.example.test/v1",
                },
                "companyProxy": {
                    "apiKey": "sk-company",
                },
            },
        }
    )

    with pytest.raises(ValueError, match="Provider 'companyProxy' requires api_base"):
        make_provider(config)


@pytest.fixture
def mock_agent_runtime(tmp_path):
    """Mock agent command dependencies for focused CLI tests."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "default-workspace")

    with patch("nanobot.config.loader.load_config", return_value=config) as mock_load_config, \
         patch("nanobot.config.loader.resolve_config_env_vars", side_effect=lambda c: c), \
         patch("nanobot.cli.agent.sync_workspace_templates") as mock_sync_templates, \
         patch("nanobot.providers.factory.make_provider", return_value=_fake_provider()), \
         patch("nanobot.cli.terminal._print_agent_response") as mock_print_response, \
         patch("nanobot.bus.queue.MessageBus"), \
         patch("nanobot.cli.agent.AgentLoop.from_config") as mock_from_config:
        agent_loop = MagicMock()
        agent_loop.process_direct = AsyncMock(
            return_value=OutboundMessage(channel="cli", chat_id="direct", content="mock-response"),
        )
        agent_loop.aclose = AsyncMock(return_value=None)
        mock_from_config.return_value = agent_loop

        yield {
            "config": config,
            "load_config": mock_load_config,
            "sync_templates": mock_sync_templates,
            "from_config": mock_from_config,
            "agent_loop": agent_loop,
            "print_response": mock_print_response,
        }


def test_agent_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output
    assert "--theme" not in stripped_output


def test_agent_uses_default_config_when_no_workspace_or_config_flags(mock_agent_runtime):
    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (None,)
    assert mock_agent_runtime["sync_templates"].call_args.args == (
        mock_agent_runtime["config"].workspace_path,
    )
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == mock_agent_runtime["config"].workspace_path
    assert "cron_service" not in mock_agent_runtime["from_config"].call_args.kwargs
    mock_agent_runtime["agent_loop"].process_direct.assert_awaited_once()
    mock_agent_runtime["print_response"].assert_called_once_with(
        "mock-response", render_markdown=True, metadata={},
    )


def test_agent_uses_explicit_config_path(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)


def test_agent_config_sets_active_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "nanobot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.cli.agent.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("nanobot.providers.factory.make_provider", lambda _config: _fake_provider())
    monkeypatch.setattr("nanobot.bus.queue.MessageBus", lambda: object())

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(channel="cli", chat_id="direct", content="ok")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("nanobot.cli.agent.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("nanobot.cli.terminal._print_agent_response", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["config_path"] == config_file.resolve()


def test_agent_overrides_workspace_path(mock_agent_runtime):
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(app, ["agent", "-m", "hello", "-w", str(workspace_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == workspace_path


def test_agent_workspace_override_wins_over_config_workspace(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_path), "-w", str(workspace_path)],
    )

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == workspace_path
