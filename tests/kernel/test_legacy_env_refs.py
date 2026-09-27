"""LEGACY_ENV_REFS: every credential ref maps to the env var(s) the pre-kernel code read.

Legacy hosts (LegacyEnvironment) keep working with the same environment variables;
kernel-native hosts get none of them (see test_credentials_scope.py).
"""

from __future__ import annotations

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.env import resolve_credential
from nanobot.kernel.legacy import LEGACY_ENV_REFS, LegacyEnvironment, legacy_scope_for_ref

# Pinned from the os.environ reads removed in Task 4 (file: old variable names).
EXPECTED = {
    # agent/tools/web.py
    "web/brave": ("BRAVE_API_KEY",),
    "web/tavily": ("TAVILY_API_KEY",),
    "web/searxng/base_url": ("SEARXNG_BASE_URL",),
    "web/jina": ("JINA_API_KEY",),
    "web/kagi": ("KAGI_API_KEY",),
    "web/exa": ("EXA_API_KEY",),
    "web/olostep": ("OLOSTEP_API_KEY",),
    "web/bocha": ("BOCHA_API_KEY",),
    "web/volcengine": ("VOLCENGINE_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"),
    "web/serper": ("SERPER_API_KEY",),
    "web/keenable": ("KEENABLE_API_KEY",),
    "web/anysearch": ("ANYSEARCH_API_KEY",),
    # providers/transcription.py
    "transcription/assemblyai": ("ASSEMBLYAI_API_KEY",),
    "transcription/assemblyai/base_url": ("ASSEMBLYAI_BASE_URL",),
    "transcription/openai": ("OPENAI_API_KEY",),
    "transcription/openai/base_url": ("OPENAI_TRANSCRIPTION_BASE_URL",),
    "transcription/groq": ("GROQ_API_KEY",),
    "transcription/groq/base_url": ("GROQ_BASE_URL",),
    "transcription/openrouter": ("OPENROUTER_API_KEY",),
    "transcription/openrouter/base_url": ("OPENROUTER_BASE_URL",),
    "transcription/mimo": ("MIMO_API_KEY",),
    "transcription/mimo/base_url": ("MIMO_API_BASE",),
    "transcription/stepfun": ("STEPFUN_API_KEY",),
    # utils/searchusage.py
    "searchusage/tavily": ("TAVILY_API_KEY",),
    # providers/openai_compat_provider.py
    "observability/langfuse_secret": ("LANGFUSE_SECRET_KEY",),
    # providers/bedrock_provider.py
    "providers/bedrock/region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
    # providers/github_copilot_provider.py (endpoint overrides, not secrets)
    "providers/github_copilot/api_base": ("NANOBOT_COPILOT_BASE_URL",),
    "providers/github_copilot/token_url": ("NANOBOT_COPILOT_TOKEN_URL",),
    "providers/github_copilot/client_id": ("NANOBOT_GITHUB_COPILOT_CLIENT_ID",),
    "providers/github_copilot/device_code_url": ("NANOBOT_GITHUB_DEVICE_CODE_URL",),
    "providers/github_copilot/access_token_url": ("NANOBOT_GITHUB_ACCESS_TOKEN_URL",),
    "providers/github_copilot/user_url": ("NANOBOT_GITHUB_USER_URL",),
}

_ALL_NAMES = sorted({name for names in EXPECTED.values() for name in names})


def _config(tmp_path, **search):
    data = {"agents": {"defaults": {"workspace": str(tmp_path)}}}
    if search:
        data["tools"] = {"web": {"search": search}}
    return Config.model_validate(data)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_table_is_pinned():
    assert LEGACY_ENV_REFS == EXPECTED


@pytest.mark.parametrize(("ref", "names"), sorted(EXPECTED.items()))
def test_each_ref_reads_its_legacy_env_var(ref, names, tmp_path, clean_env):
    clean_env.setenv(names[0], f"value-of-{names[0]}")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    scope = legacy_scope_for_ref(ref)
    assert env.credentials.resolve(ref, scope) == f"value-of-{names[0]}"
    # Direct construction without an env keeps the legacy ambient fallback.
    assert resolve_credential(None, ref, scope) == f"value-of-{names[0]}"


@pytest.mark.parametrize(("ref", "names"), [(r, n) for r, n in EXPECTED.items() if len(n) > 1])
def test_fallback_names_apply_in_order(ref, names, tmp_path, clean_env):
    clean_env.setenv(names[1], "second")
    scope = legacy_scope_for_ref(ref)
    assert LegacyEnvironment.from_config(_config(tmp_path)).credentials.resolve(ref, scope) == (
        "second"
    )
    clean_env.setenv(names[0], "first")
    assert LegacyEnvironment.from_config(_config(tmp_path)).credentials.resolve(ref, scope) == (
        "first"
    )


def test_unset_or_empty_env_var_yields_no_ref(tmp_path, clean_env):
    clean_env.setenv("BRAVE_API_KEY", "")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert env.credentials.resolve("web/brave", "tool:web") is None
    assert env.credentials.resolve("web/kagi", "tool:web") is None


def test_config_value_wins_over_env(tmp_path, clean_env):
    clean_env.setenv("BRAVE_API_KEY", "from-env")
    env = LegacyEnvironment.from_config(_config(tmp_path, provider="brave", apiKey="from-config"))
    assert env.credentials.resolve("web/brave", "tool:web") == "from-config"


def test_env_values_never_appear_in_repr(tmp_path, clean_env):
    clean_env.setenv("TAVILY_API_KEY", "tvly-REPR-SECRET")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert "tvly-REPR-SECRET" not in repr(env) + repr(env.credentials)
