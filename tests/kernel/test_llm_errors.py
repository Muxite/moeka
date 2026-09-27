"""classify(): error LLMResponses (built by the real provider error paths) -> typed errors."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import openai
import pytest

from nanobot.kernel.llm_errors import (
    AuthError,
    ContentFilterError,
    LLMError,
    LLMTimeoutError,
    ModelNotFound,
    ParseError,
    QuotaError,
    RateLimitError,
    TransientError,
    UnsupportedRequestError,
    classify,
)
from nanobot.providers.anthropic_provider import AnthropicProvider
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

_REQUEST = httpx.Request("POST", "https://api.example.test/v1/chat/completions")


def _http(status: int, *, headers: dict[str, str] | None = None, body: Any = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers, request=_REQUEST)
    return httpx.Response(status, headers=headers, json=body, request=_REQUEST)


def _openai(cls: type[openai.APIStatusError], status: int, body: Any = None,
            headers: dict[str, str] | None = None) -> LLMResponse:
    exc = cls(
        f"Error code: {status}", response=_http(status, headers=headers, body=body), body=body,
    )
    return OpenAICompatProvider._handle_error(exc)


def _anthropic(cls: type[anthropic.APIStatusError], status: int, body: Any = None,
               headers: dict[str, str] | None = None) -> LLMResponse:
    exc = cls(
        f"Error code: {status}", response=_http(status, headers=headers, body=body), body=body,
    )
    return AnthropicProvider._handle_error(exc)


CASES: list[tuple[str, LLMResponse, type[LLMError], bool, int | None]] = [
    (
        "openai 401",
        _openai(openai.AuthenticationError, 401,
                {"error": {"type": "invalid_request_error", "code": "invalid_api_key"}}),
        AuthError, False, 401,
    ),
    (
        "anthropic 401",
        _anthropic(anthropic.AuthenticationError, 401,
                   {"type": "error", "error": {"type": "authentication_error"}}),
        AuthError, False, 401,
    ),
    (
        "openai 402 empty body (credits)",
        _openai(openai.APIStatusError, 402),
        QuotaError, False, 402,
    ),
    (
        "openrouter 402 credits body",
        _openai(openai.APIStatusError, 402,
                {"error": {"code": 402, "message": "Insufficient credits"}}),
        QuotaError, False, 402,
    ),
    (
        "openai 429 insufficient_quota",
        _openai(openai.RateLimitError, 429,
                {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}),
        QuotaError, False, 429,
    ),
    (
        "exception text out of credits (no status)",
        OpenAICompatProvider._handle_error(Exception("You are out of credits")),
        QuotaError, False, None,
    ),
    (
        "openai 429 rate limit",
        _openai(openai.RateLimitError, 429,
                {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}},
                headers={"retry-after": "7"}),
        RateLimitError, True, 429,
    ),
    (
        "anthropic 429 rate limit",
        _anthropic(anthropic.RateLimitError, 429,
                   {"type": "error", "error": {"type": "rate_limit_error"}},
                   headers={"retry-after": "3"}),
        RateLimitError, True, 429,
    ),
    ("openai 500", _openai(openai.InternalServerError, 500), TransientError, True, 500),
    ("openai 502", _openai(openai.APIStatusError, 502), TransientError, True, 502),
    (
        "anthropic 503 overloaded",
        _anthropic(anthropic.APIStatusError, 503,
                   {"type": "error", "error": {"type": "overloaded_error"}}),
        TransientError, True, 503,
    ),
    (
        "openai connection error",
        OpenAICompatProvider._handle_error(openai.APIConnectionError(request=_REQUEST)),
        TransientError, True, None,
    ),
    (
        "openai timeout",
        OpenAICompatProvider._handle_error(openai.APITimeoutError(request=_REQUEST)),
        LLMTimeoutError, True, None,
    ),
    (
        "anthropic stream stall",
        LLMResponse(
            content="Error calling LLM: stream stalled for more than 90 seconds",
            finish_reason="error",
            error_kind="timeout",
        ),
        LLMTimeoutError, True, None,
    ),
    (
        "unexpected exception timeout",
        LLMProvider._error_response_from_exception(TimeoutError("read timed out")),
        LLMTimeoutError, True, None,
    ),
    (
        "openai 404 model_not_found",
        _openai(openai.NotFoundError, 404,
                {"error": {"type": "invalid_request_error", "code": "model_not_found"}}),
        ModelNotFound, False, 404,
    ),
    (
        "anthropic 404 unknown model",
        _anthropic(anthropic.NotFoundError, 404,
                   {"type": "error", "error": {"type": "not_found_error",
                                               "message": "model: claude-nope"}}),
        ModelNotFound, False, 404,
    ),
]


@pytest.mark.parametrize(
    ("response", "expected", "retryable", "status"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_classification_table(response, expected, retryable, status) -> None:
    error = classify(response, call_id="c1", model="m", provider="p")

    assert type(error) is expected
    assert error.retryable is retryable
    assert error.status == status
    assert (error.call_id, error.model, error.provider) == ("c1", "m", "p")
    assert error.raw == response.content


def test_rate_limit_carries_retry_after() -> None:
    response = _openai(
        openai.RateLimitError, 429,
        {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}},
        headers={"retry-after": "7"},
    )
    error = classify(response)
    assert isinstance(error, RateLimitError)
    assert error.retry_after == 7
    assert error.kind == "rate_limit"


def test_content_filter_finish_reason() -> None:
    raw = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None),
            finish_reason="content_filter",
        )],
        usage=None,
    )
    response = OpenAICompatProvider(api_key="k")._parse(raw)
    assert response.finish_reason == "content_filter"

    error = classify(response)
    assert isinstance(error, ContentFilterError)
    assert error.retryable is False


@pytest.mark.parametrize("finish_reason", ["stop", "length", "tool_calls"])
def test_non_error_response_is_none(finish_reason: str) -> None:
    assert classify(LLMResponse(content="ok", finish_reason=finish_reason)) is None


def test_4xx_body_mentioning_500_is_not_retryable() -> None:
    response = _openai(
        openai.BadRequestError, 400,
        {"error": {"message": "max_tokens 5000 exceeds limit; timeout"}},
    )
    error = classify(response)
    assert type(error) is LLMError
    assert error.kind == "invalid_request"
    assert error.retryable is False


def test_error_attributes_and_hierarchy() -> None:
    timeout = LLMTimeoutError(model="m")
    assert isinstance(timeout, TimeoutError) and isinstance(timeout, LLMError)
    assert timeout.retryable is True

    parse = ParseError(raw="{bad", attempts=3, model="m")
    assert (parse.raw, parse.attempts, parse.kind) == ("{bad", 3, "parse")

    unsupported = UnsupportedRequestError(fields=["seed"], provider="anthropic")
    assert unsupported.fields == ("seed",)
    assert "seed" in str(unsupported)
