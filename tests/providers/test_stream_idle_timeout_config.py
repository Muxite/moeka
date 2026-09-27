from __future__ import annotations

from typing import Any

import pytest

from nanobot.providers.base import (
    DEFAULT_STREAM_IDLE_TIMEOUT_S,
    MAX_STREAM_IDLE_TIMEOUT_S,
    resolve_stream_idle_timeout_s,
)
from nanobot.providers.bedrock_provider import BedrockProvider


class _BedrockClient:
    def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
        return {"stream": iter([
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ])}


def test_stream_idle_timeout_parser_rejects_invalid_values() -> None:
    assert resolve_stream_idle_timeout_s(env_value="abc") == DEFAULT_STREAM_IDLE_TIMEOUT_S
    assert resolve_stream_idle_timeout_s(env_value="-1") == DEFAULT_STREAM_IDLE_TIMEOUT_S
    assert resolve_stream_idle_timeout_s(env_value="0") == DEFAULT_STREAM_IDLE_TIMEOUT_S


def test_stream_idle_timeout_parser_accepts_and_clamps_numeric_values() -> None:
    assert resolve_stream_idle_timeout_s(env_value="1.5") == 1.5
    assert resolve_stream_idle_timeout_s(env_value="7200") == MAX_STREAM_IDLE_TIMEOUT_S


_FACTORY_CONFIGS = {
    "openai_compat": ({"openai": {"apiKey": "sk-test"}}, "openai/gpt-4o"),
    "anthropic": ({"anthropic": {"apiKey": "sk-test"}}, "anthropic/claude-sonnet-4"),
    "bedrock": ({"bedrock": {"region": "us-east-1"}}, "bedrock/global.anthropic.claude-opus-4-7"),
}


def _factory_config(tmp_path, backend: str):
    from nanobot.config.schema import Config

    providers, model = _FACTORY_CONFIGS[backend]
    return Config.model_validate({
        "providers": providers,
        "agents": {"defaults": {"workspace": str(tmp_path), "model": model}},
    })


@pytest.mark.parametrize("backend", sorted(_FACTORY_CONFIGS))
def test_legacy_invalid_idle_timeout_env_falls_back_per_backend(
    tmp_path, monkeypatch, backend
) -> None:
    """Legacy host: an invalid NANOBOT_STREAM_IDLE_TIMEOUT_S gives the default timeout."""
    from nanobot.kernel.legacy import LegacyEnvironment
    from nanobot.providers.factory import make_provider

    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "abc")
    config = _factory_config(tmp_path, backend)
    provider = make_provider(config, env=LegacyEnvironment.from_config(config))
    assert provider.stream_idle_timeout_s == DEFAULT_STREAM_IDLE_TIMEOUT_S


def _bedrock_read_timeout(provider: BedrockProvider) -> float:
    return provider._client.meta.config.read_timeout


def test_bedrock_client_timeout_honours_legacy_env_var(tmp_path, monkeypatch) -> None:
    """The botocore socket timeout is built from the host setting, not the class default."""
    from nanobot.kernel.legacy import LegacyEnvironment
    from nanobot.providers.factory import make_provider

    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "300")
    config = _factory_config(tmp_path, "bedrock")
    provider = make_provider(config, env=LegacyEnvironment.from_config(config))
    assert isinstance(provider, BedrockProvider)
    assert provider.stream_idle_timeout_s == 300.0
    assert _bedrock_read_timeout(provider) == 300.0
    assert provider._client.meta.config.connect_timeout == 300.0


def test_bedrock_client_timeout_uses_kernel_runtime_value(tmp_path, monkeypatch) -> None:
    from nanobot.providers.factory import make_provider
    from tests._kernel_env import DictConfigSource, credential_env

    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "300")  # ignored by a kernel host
    config = _factory_config(tmp_path, "bedrock")
    kernel = credential_env(
        root=tmp_path / "host",
        config=DictConfigSource({"runtime": {"stream_idle_timeout_s": 240}}),
    )
    provider = make_provider(config, env=kernel)
    assert _bedrock_read_timeout(provider) == 240.0

    default = make_provider(config, env=credential_env(root=tmp_path / "host2"))
    assert _bedrock_read_timeout(default) == DEFAULT_STREAM_IDLE_TIMEOUT_S


@pytest.mark.asyncio
async def test_bedrock_injected_client_still_streams() -> None:
    provider = BedrockProvider(region="us-east-1", client=_BedrockClient())

    result = await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

    assert result.content == "ok"


def test_stream_idle_timeout_parser_accepts_numbers_and_unset() -> None:
    assert resolve_stream_idle_timeout_s(env_value=None) == DEFAULT_STREAM_IDLE_TIMEOUT_S
    assert resolve_stream_idle_timeout_s(env_value=2.5) == 2.5


def test_stream_idle_timeout_parser_never_reads_process_env(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "5")
    assert resolve_stream_idle_timeout_s() == DEFAULT_STREAM_IDLE_TIMEOUT_S


def test_legacy_env_invalid_idle_timeout_falls_back_to_default(tmp_path, monkeypatch) -> None:
    from nanobot.config.schema import Config
    from nanobot.kernel.legacy import LegacyEnvironment
    from nanobot.providers.factory import make_provider
    from tests._kernel_env import credential_env

    config = Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-test"}},
        "agents": {"defaults": {"workspace": str(tmp_path), "model": "openai/gpt-4o"}},
    })
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "abc")
    legacy = make_provider(config, env=LegacyEnvironment.from_config(config))
    assert legacy.stream_idle_timeout_s == DEFAULT_STREAM_IDLE_TIMEOUT_S

    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "7200")
    legacy = make_provider(config, env=LegacyEnvironment.from_config(config))
    assert legacy.stream_idle_timeout_s == MAX_STREAM_IDLE_TIMEOUT_S
    kernel = make_provider(config, env=credential_env(root=tmp_path / "host"))
    assert kernel.stream_idle_timeout_s == DEFAULT_STREAM_IDLE_TIMEOUT_S
