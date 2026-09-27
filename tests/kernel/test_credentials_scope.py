"""Credential scopes (I1): a scope reads only its own refs; nothing ambient for kernel hosts."""

from __future__ import annotations

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.env import missing_credential, resolve_credential
from nanobot.kernel.legacy import LegacyEnvironment
from tests._kernel_env import credential_env

PROVIDER_SECRET = "sk-or-SCOPE-TEST"
SEARCH_SECRET = "brave-SCOPE-TEST"


def _legacy(tmp_path, monkeypatch, **env_vars):
    for name, value in env_vars.items():
        monkeypatch.setenv(name, value)
    config = Config.model_validate({
        "providers": {"openrouter": {"apiKey": PROVIDER_SECRET}},
        "tools": {
            "web": {"search": {"provider": "brave", "apiKey": SEARCH_SECRET}},
            "exec": {"allowedEnvKeys": ["SCOPE_TEST_ALLOWED"]},
        },
        "agents": {"defaults": {"workspace": str(tmp_path)}},
    })
    return LegacyEnvironment.from_config(config)


def test_provider_ref_is_readable_only_by_its_provider_scope(tmp_path, monkeypatch):
    env = _legacy(tmp_path, monkeypatch)
    ref = "providers/openrouter/api_key"
    assert env.credentials.resolve(ref, "provider:openrouter") == PROVIDER_SECRET
    for other in ("provider:openai", "tool:web", "exec", "transcription", "searchusage"):
        assert env.credentials.resolve(ref, other) is None


def test_web_ref_is_not_readable_by_provider_or_exec_scopes(tmp_path, monkeypatch):
    env = _legacy(tmp_path, monkeypatch)
    assert env.credentials.resolve("web/brave", "tool:web") == SEARCH_SECRET
    assert env.credentials.resolve("web/brave", "provider:openrouter") is None
    assert env.credentials.resolve("web/brave", "exec") is None


def test_env_sourced_refs_keep_their_scope(tmp_path, monkeypatch):
    env = _legacy(tmp_path, monkeypatch, TAVILY_API_KEY="tvly-SCOPE", GROQ_API_KEY="gsk-SCOPE")
    assert env.credentials.resolve("searchusage/tavily", "searchusage") == "tvly-SCOPE"
    assert env.credentials.resolve("searchusage/tavily", "tool:web") is None
    assert env.credentials.resolve("web/tavily", "tool:web") == "tvly-SCOPE"
    assert env.credentials.resolve("transcription/groq", "transcription") == "gsk-SCOPE"
    assert env.credentials.resolve("transcription/groq", "tool:web") is None


def test_exec_scope_reads_process_env_names_only(tmp_path, monkeypatch):
    env = _legacy(tmp_path, monkeypatch, SCOPE_TEST_ALLOWED="exec-value")
    assert env.credentials.resolve("SCOPE_TEST_ALLOWED", "exec") == "exec-value"
    # exec cannot reach a named credential, and other scopes cannot read env names.
    assert env.credentials.resolve("providers/openrouter/api_key", "exec") is None
    assert env.credentials.resolve("SCOPE_TEST_ALLOWED", "tool:web") is None
    assert env.credentials.resolve("SCOPE_TEST_ALLOWED", "provider:openrouter") is None


def test_unknown_ref_returns_none(tmp_path, monkeypatch):
    env = _legacy(tmp_path, monkeypatch)
    assert env.credentials.resolve("web/nonexistent", "tool:web") is None
    assert env.credentials.resolve("nope", "exec") is None
    assert credential_env().credentials.resolve("web/brave", "tool:web") is None


def test_kernel_env_with_empty_resolver_never_reads_process_env(monkeypatch):
    for name in ("BRAVE_API_KEY", "TAVILY_API_KEY", "OPENAI_API_KEY", "LANGFUSE_SECRET_KEY",
                 "AWS_REGION", "NANOBOT_COPILOT_BASE_URL", "KERNEL_EXEC_PROBE"):
        monkeypatch.setenv(name, f"ambient-{name}")
    env = credential_env()
    for ref, scope in (
        ("web/brave", "tool:web"),
        ("web/tavily", "tool:web"),
        ("searchusage/tavily", "searchusage"),
        ("transcription/openai", "transcription"),
        ("observability/langfuse_secret", "observability"),
        ("providers/bedrock/region", "provider:bedrock"),
        ("providers/github_copilot/api_base", "provider:github_copilot"),
        ("KERNEL_EXEC_PROBE", "exec"),
    ):
        assert env.credentials.resolve(ref, scope) is None
        assert resolve_credential(env, ref, scope) is None


@pytest.mark.parametrize("ref", ["web/brave", "providers/openai/api_key"])
def test_missing_credential_message_names_ref_only(ref):
    assert missing_credential(ref) == f"missing credential {ref}"
