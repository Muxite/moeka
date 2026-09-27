"""Regression tests for the moeka deviation that transcription providers
honour an explicit ``api_base`` argument.

Upstream nanobot took several iterations to wire per-provider Whisper
endpoints (see CLAUDE.md note: "Transcription ``api_base`` propagation").
These tests pin the precedence rules so an upstream merge that
inadvertently drops the kwarg or reorders the fallback chain fails CI.
"""
from __future__ import annotations

from nanobot.config.schema import Config
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.providers.transcription import (
    GroqTranscriptionProvider,
    OpenAITranscriptionProvider,
)
from tests._kernel_env import credential_env

_CUSTOM = "https://custom.example.test/v1/audio/transcriptions"
_DEFAULT_OPENAI = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_GROQ = "https://api.groq.com/openai/v1/audio/transcriptions"


class TestOpenAITranscriptionApiBase:
    def test_explicit_api_base_wins_over_env(self):
        env = credential_env({"transcription/openai/base_url": "https://env.example/v1"})
        provider = OpenAITranscriptionProvider(api_key="k", api_base=_CUSTOM, env=env)
        assert provider.api_url == _CUSTOM

    def test_env_used_when_no_explicit_api_base(self):
        env = credential_env({"transcription/openai/base_url": "https://env.example/v1"})
        provider = OpenAITranscriptionProvider(api_key="k", env=env)
        # Chat-style bases (no trailing /audio/transcriptions) get the path
        # appended so the request doesn't 404 (#3637).
        assert provider.api_url == "https://env.example/v1/audio/transcriptions"

    def test_default_when_no_api_base_and_no_env(self, monkeypatch):
        # A host env without the ref ignores the process variable (I1).
        monkeypatch.setenv("OPENAI_TRANSCRIPTION_BASE_URL", "https://ambient-must-be-ignored.example/v1")
        provider = OpenAITranscriptionProvider(api_key="k", env=credential_env())
        assert provider.api_url == _DEFAULT_OPENAI


class TestGroqTranscriptionApiBase:
    def test_explicit_api_base_wins_over_env(self):
        env = credential_env({"transcription/groq/base_url": "https://env.example/v1"})
        provider = GroqTranscriptionProvider(api_key="k", api_base=_CUSTOM, env=env)
        assert provider.api_url == _CUSTOM

    def test_env_used_when_no_explicit_api_base(self):
        env = credential_env({"transcription/groq/base_url": "https://env.example/v1"})
        provider = GroqTranscriptionProvider(api_key="k", env=env)
        # Chat-style bases (no trailing /audio/transcriptions) get the path
        # appended so the request doesn't 404 (#3637).
        assert provider.api_url == "https://env.example/v1/audio/transcriptions"

    def test_default_when_no_api_base_and_no_env(self, monkeypatch):
        # A host env without the ref ignores the process variable (I1).
        monkeypatch.setenv("GROQ_BASE_URL", "https://ambient-must-be-ignored.example/v1")
        provider = GroqTranscriptionProvider(api_key="k", env=credential_env())
        assert provider.api_url == _DEFAULT_GROQ


def test_legacy_env_vars_still_apply_through_legacy_environment(tmp_path, monkeypatch):
    """Legacy hosts: OPENAI_TRANSCRIPTION_BASE_URL / GROQ_API_KEY behave as before."""
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-legacy-env")
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    env = LegacyEnvironment.from_config(config)
    openai = OpenAITranscriptionProvider(api_key="k", env=env)
    assert openai.api_url == "https://env.example/v1/audio/transcriptions"
    assert GroqTranscriptionProvider(env=env).api_key == "gsk-legacy-env"


def test_direct_construction_without_env_keeps_env_var_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-direct-env")
    provider = GroqTranscriptionProvider()
    assert provider.api_url == "https://env.example/v1/audio/transcriptions"
    assert provider.api_key == "gsk-direct-env"


def test_api_key_comes_from_resolver_under_transcription_scope():
    env = credential_env(
        {"transcription/openai": "sk-resolver"},
        scopes={"transcription/openai": ["transcription"]},
    )
    assert OpenAITranscriptionProvider(env=env).api_key == "sk-resolver"
    wrong_scope = credential_env(
        {"transcription/openai": "sk-resolver"},
        scopes={"transcription/openai": ["provider:openai"]},
    )
    assert OpenAITranscriptionProvider(env=wrong_scope).api_key is None
