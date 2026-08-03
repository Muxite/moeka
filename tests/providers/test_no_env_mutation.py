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
