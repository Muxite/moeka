"""Regression tests for the moeka deviation that transcription providers
honour an explicit ``api_base`` argument.

Upstream nanobot took several iterations to wire per-provider Whisper
endpoints (see CLAUDE.md note: "Transcription ``api_base`` propagation").
These tests pin the precedence rules so an upstream merge that
inadvertently drops the kwarg or reorders the fallback chain fails CI.
"""
from __future__ import annotations

from nanobot.providers.transcription import (
    GroqTranscriptionProvider,
    OpenAITranscriptionProvider,
)

_CUSTOM = "https://custom.example.test/v1/audio/transcriptions"
_DEFAULT_OPENAI = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_GROQ = "https://api.groq.com/openai/v1/audio/transcriptions"


class TestOpenAITranscriptionApiBase:
    def test_explicit_api_base_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_TRANSCRIPTION_BASE_URL", "https://env.example/v1")
        provider = OpenAITranscriptionProvider(api_key="k", api_base=_CUSTOM)
        assert provider.api_url == _CUSTOM

    def test_env_used_when_no_explicit_api_base(self, monkeypatch):
        monkeypatch.setenv("OPENAI_TRANSCRIPTION_BASE_URL", "https://env.example/v1")
        provider = OpenAITranscriptionProvider(api_key="k")
        assert provider.api_url == "https://env.example/v1"

    def test_default_when_no_api_base_and_no_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_TRANSCRIPTION_BASE_URL", raising=False)
        provider = OpenAITranscriptionProvider(api_key="k")
        assert provider.api_url == _DEFAULT_OPENAI


class TestGroqTranscriptionApiBase:
    def test_explicit_api_base_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("GROQ_BASE_URL", "https://env.example/v1")
        provider = GroqTranscriptionProvider(api_key="k", api_base=_CUSTOM)
        assert provider.api_url == _CUSTOM

    def test_env_used_when_no_explicit_api_base(self, monkeypatch):
        monkeypatch.setenv("GROQ_BASE_URL", "https://env.example/v1")
        provider = GroqTranscriptionProvider(api_key="k")
        assert provider.api_url == "https://env.example/v1"

    def test_default_when_no_api_base_and_no_env(self, monkeypatch):
        monkeypatch.delenv("GROQ_BASE_URL", raising=False)
        provider = GroqTranscriptionProvider(api_key="k")
        assert provider.api_url == _DEFAULT_GROQ
