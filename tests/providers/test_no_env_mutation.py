"""Provider construction must never mutate the host process environment.

``OpenAICompatProvider.__init__`` used to call ``_setup_env``, which wrote the
resolved API key into ``os.environ`` — and for gateway specs *overwrote* an
existing value rather than using ``setdefault``. Six registry specs declare
``env_key="OPENAI_API_KEY"`` with ``is_gateway=True``, so configuring any of
them replaced the host's real OpenAI key with a gateway key.

That mattered in two ways. As a library (``MoekaCore``), merely constructing a
core silently re-pointed the host application's own OpenAI calls. And on a host
where ``tools.exec.allowedEnvKeys`` forwards environment variables into
agent-executed shell commands, it *materialized* a secret the operator had never
exported and handed it to arbitrary commands.

Nothing consumed those variables: every provider receives ``api_key``
explicitly from the factory, and ``FallbackProvider`` builds each fallback the
same way — so the write was vestigial and its removal changes no behavior other
than the leak.
"""

import os

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import PROVIDERS


def _spec(name: str):
    return next(s for s in PROVIDERS if s.name == name)


def test_gateway_provider_does_not_overwrite_existing_env(monkeypatch) -> None:
    """The destructive case: a gateway spec clobbering a key already set."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-REAL-OPERATOR-KEY")

    OpenAICompatProvider(api_key="sk-or-SOME-OTHER-KEY", spec=_spec("openrouter"))

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-REAL-OPERATOR-KEY"


def test_provider_does_not_introduce_env_vars(monkeypatch) -> None:
    """The leak case: a key the operator never exported must not appear."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    OpenAICompatProvider(api_key="sk-or-FROM-CONFIG-ONLY", spec=_spec("openrouter"))

    assert "OPENROUTER_API_KEY" not in os.environ


def test_non_gateway_provider_does_not_introduce_env_vars(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    OpenAICompatProvider(api_key="sk-deepseek-FROM-CONFIG", spec=_spec("deepseek"))

    assert "DEEPSEEK_API_KEY" not in os.environ


def test_construction_leaves_environment_byte_identical() -> None:
    """Catch-all: no spec may add, remove, or change any variable."""
    before = dict(os.environ)

    for name in ("openrouter", "deepseek", "zhipu", "siliconflow", "byteplus"):
        OpenAICompatProvider(api_key="sk-test-key", spec=_spec(name))

    assert dict(os.environ) == before


def test_factory_with_host_env_leaves_environment_byte_identical(tmp_path) -> None:
    from nanobot.config.schema import Config
    from nanobot.providers.factory import make_provider
    from tests._kernel_env import credential_env

    config = Config.model_validate({"agents": {"defaults": {
        "workspace": str(tmp_path), "model": "openrouter/auto", "provider": "openrouter",
    }}})
    before = dict(os.environ)
    make_provider(config, env=credential_env({"providers/openrouter/api_key": "sk-or-RESOLVED"}))
    assert dict(os.environ) == before


def test_bedrock_bearer_token_is_passed_explicitly_not_via_environment(monkeypatch) -> None:
    """R5: the Bedrock API key reaches boto as an explicit bearer token, never os.environ."""
    import io

    import pytest

    pytest.importorskip("boto3")
    from botocore.awsrequest import AWSResponse

    from nanobot.providers.bedrock_provider import BedrockProvider

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    before = dict(os.environ)
    provider = BedrockProvider(api_key="bedrock-TOKEN-explicit", region="us-east-1")
    assert dict(os.environ) == before

    seen: dict[str, object] = {}
    body = (
        b'{"output":{"message":{"role":"assistant","content":[{"text":"hi"}]}},'
        b'"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},'
        b'"metrics":{"latencyMs":1}}'
    )

    class _Raw:
        def stream(self, **kwargs):
            yield io.BytesIO(body).read()

    def before_send(request, **kwargs):
        seen["auth"] = request.headers.get("Authorization")
        response = AWSResponse(request.url, 200, {"content-type": "application/json"}, _Raw())
        response._content = body
        return response

    provider._client.meta.events.register("before-send", before_send)
    provider._client.converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])
    assert seen["auth"] in (b"Bearer bedrock-TOKEN-explicit", "Bearer bedrock-TOKEN-explicit")
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
