import json

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app

runner = CliRunner()

_ANTHROPIC_BACKEND_CASES = (
    ("anthropic", "anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY", "Anthropic"),
    ("kimi_coding", "kimiCoding", "kimi-for-coding", "KIMI_CODING_API_KEY", "Kimi Coding"),
    (
        "minimax_anthropic",
        "minimaxAnthropic",
        "MiniMax-M2.7-highspeed",
        "MINIMAX_API_KEY",
        "MiniMax (Anthropic)",
    ),
)


def _without_rendered_line_breaks(output: str) -> str:
    return "".join(output.splitlines())


def _write_ready_config(config_path, *, channels: dict | None = None) -> None:
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "ollama/llama3.2",
                        "provider": "ollama",
                    }
                },
                "providers": {
                    "ollama": {
                        "apiBase": "http://localhost:11434/v1",
                    }
                },
                "channels": channels or {},
            }
        ),
        encoding="utf-8",
    )


def test_status_reports_ready_provider_and_next_step(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    _write_ready_config(config_path)

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Agent: ✓ provider/model configuration is ready" in result.stdout
    assert "Ollama:" in result.stdout
    assert "Model: ollama/llama3.2" in result.stdout
    assert 'nanobot agent -m "Hello!"' in result.stdout
    assert "Status does not call the model" in result.stdout


def test_status_validates_bedrock_without_constructing_provider(
    tmp_path,
    monkeypatch,
) -> None:
    from nanobot.providers.bedrock_provider import BedrockProvider

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "bedrock/amazon.nova-lite-v1:0",
                        "provider": "bedrock",
                    }
                },
                "providers": {"bedrock": {"region": "us-east-1"}},
            }
        ),
        encoding="utf-8",
    )

    def _unexpected_init(*_args, **_kwargs) -> None:
        pytest.fail("status must not construct a provider client")

    monkeypatch.setattr(BedrockProvider, "__init__", _unexpected_init)

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Agent: ✓ provider/model configuration is ready" in result.stdout
    assert "Status does not call the model or verify network access" in result.stdout


@pytest.mark.parametrize(
    ("provider", "provider_key", "model", "env_name", "label"),
    _ANTHROPIC_BACKEND_CASES,
)
def test_status_reports_missing_key_for_anthropic_backends(
    tmp_path,
    monkeypatch,
    provider: str,
    provider_key: str,
    model: str,
    env_name: str,
    label: str,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(env_name, raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": model, "provider": provider}},
                "providers": {provider_key: {}},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    output = _without_rendered_line_breaks(result.stdout)

    assert result.exit_code == 0
    assert f"Agent: ✗ No API key configured for provider '{provider}'." in output
    assert f"{label}: not set" in output
    assert "provider/model configuration is ready" not in output
    assert 'Next: nanobot agent -m "Hello!"' not in output
    assert "nanobot provider login" in output


@pytest.mark.parametrize(
    ("provider", "provider_key", "model", "env_name", "label"),
    _ANTHROPIC_BACKEND_CASES,
)
def test_status_accepts_resolved_key_for_anthropic_backends(
    tmp_path,
    monkeypatch,
    provider: str,
    provider_key: str,
    model: str,
    env_name: str,
    label: str,
) -> None:
    monkeypatch.setenv(env_name, "test-api-key")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": model, "provider": provider}},
                "providers": {provider_key: {"apiKey": f"${{{env_name}}}"}},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Agent: ✓ provider/model configuration is ready" in result.stdout
    assert f"{label}: ✓" in result.stdout
    assert 'nanobot agent -m "Hello!"' in result.stdout


def test_status_reports_missing_provider_with_shortest_setup_routes(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Agent: ✗" in result.stdout
    assert "No provider is configured for model" in result.stdout
    output = _without_rendered_line_breaks(result.stdout)
    assert "agents.defaults.model" in output
    assert "nanobot provider login" in output
    assert "nanobot status --config" in output
    assert "nanobot onboard" not in output
    assert "nanobot webui" not in output


def test_status_readiness_does_not_validate_channel_configuration(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    _write_ready_config(
        config_path,
        channels={"websocket": {"enabled": False, "path": "missing-slash"}},
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Agent: ✓ provider/model configuration is ready" in result.stdout
    assert "channels.websocket" not in result.stdout


def test_status_reports_json_location_without_traceback(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{broken", encoding="utf-8")

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "Invalid configuration" in result.stdout
    assert "JSON syntax error at line 1, column 2" in result.stdout
    assert "Traceback" not in result.stdout


def test_status_reports_field_without_exposing_secret(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    secret = "should-never-appear"
    config_path.write_text(
        json.dumps({"providers": {"openrouter": {"apiKey": [secret]}}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "providers.openrouter.apiKey" in result.stdout
    assert secret not in result.stdout
    assert "input_value" not in result.stdout
    assert "errors.pydantic.dev" not in result.stdout


def test_status_reports_missing_env_var_at_field(tmp_path, monkeypatch) -> None:
    """moeka deviation: upstream surfaces the field path from a raised
    ConfigLoadError here; moeka never raises for a missing ${VAR} (it warns
    at load time — see nanobot/config/loader.py:resolve_config_env_vars),
    so `status` just falls through to reporting the provider as not
    configured rather than showing the field path in its own output."""
    from loguru import logger as loguru_logger

    name = "NANOBOT_TEST_STATUS_MISSING"
    monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"providers": {"openrouter": {"apiKey": f"${{{name}}}"}}}),
        encoding="utf-8",
    )

    records: list[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        result = runner.invoke(app, ["status", "--config", str(config_path)])
    finally:
        loguru_logger.remove(handler_id)

    assert result.exit_code == 0
    assert "OpenRouter: not set" in result.stdout
    assert "OpenRouter: ✓" not in result.stdout
    assert any(name in r and "providers.openrouter.api_key" in r for r in records)


def test_agent_reports_malformed_environment_config_without_traceback(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "missing.json"
    invalid_value = "sensitive-not-json"
    monkeypatch.setenv("NANOBOT_PROVIDERS", invalid_value)
    # No config file: the default path is missing, so Config() parses NANOBOT_* env.
    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)

    result = runner.invoke(app, ["agent", "--message", "hello"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Environment-based configuration could not be parsed" in result.stdout
    assert "nanobot status --config" in result.stdout
    assert invalid_value not in result.stdout
    assert not config_path.exists()


def test_agent_points_invalid_config_to_status(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{broken", encoding="utf-8")

    result = runner.invoke(app, ["agent", "--message", "hello", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "Invalid configuration" in result.stdout
    assert "nanobot status --config" in result.stdout
    assert "Traceback" not in result.stdout


def test_agent_provider_setup_failure_points_to_shortest_routes(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(workspace)}}}),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["agent", "--message", "hello", "--config", str(config_path)],
    )
    output = _without_rendered_line_breaks(result.stdout)

    assert result.exit_code == 1
    assert "Agent cannot start: No provider is configured for model" in output
    assert "nanobot provider login" in output
    assert "nanobot onboard" not in output
    assert "nanobot status --config" in output
    assert "Traceback" not in output
    assert not workspace.exists()



def test_status_missing_file_points_to_setup_without_changing_exit_contract(tmp_path) -> None:
    config_path = tmp_path / "missing.json"

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "configuration file not found" in result.stdout
    output = _without_rendered_line_breaks(result.stdout)
    assert "nanobot provider login" in output
    assert "nanobot webui" not in output
    assert "nanobot onboard" not in output
