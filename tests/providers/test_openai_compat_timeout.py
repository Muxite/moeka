from unittest.mock import patch, sentinel

from nanobot.providers import openai_compat_provider
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import ProviderSpec


def _assert_openai_compat_timeout(timeout) -> None:
    assert timeout == 120.0


async def test_openai_compat_provider_defers_sdk_client_until_first_use() -> None:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        mock_async_openai.assert_not_called()
        await provider._ensure_client()

    kwargs = mock_async_openai.call_args.kwargs
    _assert_openai_compat_timeout(kwargs["timeout"])
    # Cloud endpoints pass http_client=None so the SDK creates its own
    # DefaultAsyncHttpxClient, which already handles proxy env vars,
    # connection limits, and redirects correctly.
    assert kwargs["http_client"] is None


async def test_openai_compat_provider_sets_timeout_on_local_http_client() -> None:
    spec = ProviderSpec(
        name="local",
        keywords=(),
        env_key="",
        is_local=True,
        default_api_base="http://127.0.0.1:11434/v1",
    )

    with (
        patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai,
        patch(
            "httpx.AsyncClient",
            return_value=sentinel.http_client,
        ) as mock_http_client,
    ):
        provider = OpenAICompatProvider(spec=spec)
        mock_async_openai.assert_not_called()
        await provider._ensure_client()

    client_kwargs = mock_http_client.call_args.kwargs
    _assert_openai_compat_timeout(client_kwargs["timeout"])
    assert client_kwargs["limits"].keepalive_expiry == 0

    openai_kwargs = mock_async_openai.call_args.kwargs
    _assert_openai_compat_timeout(openai_kwargs["timeout"])
    assert openai_kwargs["http_client"] is sentinel.http_client


async def test_openai_compat_provider_timeout_can_be_overridden_by_env(
    tmp_path, monkeypatch
) -> None:
    """Legacy hosts: NANOBOT_OPENAI_COMPAT_TIMEOUT_S via LegacyEnvironment; kernel hosts: default."""
    from nanobot.config.schema import Config
    from nanobot.kernel.legacy import LegacyEnvironment
    from nanobot.providers.factory import make_provider
    from tests._kernel_env import credential_env

    monkeypatch.setenv("NANOBOT_OPENAI_COMPAT_TIMEOUT_S", "45")
    config = Config.model_validate({
        "providers": {"custom": {"apiKey": "test-key", "apiBase": "https://example.com/v1"}},
        "agents": {"defaults": {"workspace": str(tmp_path), "model": "custom/gpt-4o"}},
    })

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = make_provider(config, env=LegacyEnvironment.from_config(config))
        await provider._ensure_client()
    assert mock_async_openai.call_args.kwargs["timeout"] == 45.0

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = make_provider(config, env=credential_env(root=tmp_path / "host"))
        await provider._ensure_client()
    _assert_openai_compat_timeout(mock_async_openai.call_args.kwargs["timeout"])

    # A direct construction (no factory, no env) keeps the default.
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        await provider._ensure_client()
    _assert_openai_compat_timeout(mock_async_openai.call_args.kwargs["timeout"])


async def test_missing_langfuse_warning_recommends_plugin_command(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(openai_compat_provider, "AsyncOpenAI", None)

    with (
        patch("importlib.util.find_spec", return_value=None),
        patch("openai.AsyncOpenAI") as mock_async_openai,
        patch("nanobot.providers.openai_compat_provider.logger.warning") as mock_warning,
    ):
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        await provider._ensure_client()

    mock_warning.assert_called_once_with(
        "LANGFUSE_SECRET_KEY is set but langfuse is not installed; "
        "run `nanobot plugins enable langfuse` to enable tracing"
    )
    mock_async_openai.assert_called_once()


async def test_langfuse_secret_comes_from_resolver(monkeypatch) -> None:
    from tests._kernel_env import credential_env

    monkeypatch.setattr(openai_compat_provider, "AsyncOpenAI", None)
    env = credential_env({"observability/langfuse_secret": "lf-secret"})
    with (
        patch("importlib.util.find_spec", return_value=None),
        patch("openai.AsyncOpenAI"),
        patch("nanobot.providers.openai_compat_provider.logger.warning") as mock_warning,
    ):
        provider = OpenAICompatProvider(
            api_key="test-key", api_base="https://example.com/v1", env=env
        )
        await provider._ensure_client()
    mock_warning.assert_called_once()
    assert "lf-secret" not in str(mock_warning.call_args)


async def test_langfuse_ambient_secret_ignored_under_host_env(monkeypatch) -> None:
    from tests._kernel_env import credential_env

    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "ambient-must-be-ignored")
    monkeypatch.setattr(openai_compat_provider, "AsyncOpenAI", None)
    with (
        patch("importlib.util.find_spec", return_value=None),
        patch("openai.AsyncOpenAI"),
        patch("nanobot.providers.openai_compat_provider.logger.warning") as mock_warning,
    ):
        provider = OpenAICompatProvider(
            api_key="test-key", api_base="https://example.com/v1", env=credential_env()
        )
        await provider._ensure_client()
    mock_warning.assert_not_called()
