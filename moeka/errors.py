"""Typed errors raised by model calls; ``kind`` is the stable category."""

from nanobot.kernel.llm_errors import (
    AuthError,
    BudgetExceeded,
    ContentFilterError,
    LLMError,
    LLMTimeoutError,
    ModelNotFound,
    ParseError,
    QuotaError,
    RateLimitError,
    TransientError,
    TruncatedError,
    UnsupportedRequestError,
)

__all__ = [
    "AuthError",
    "BudgetExceeded",
    "ContentFilterError",
    "LLMError",
    "LLMTimeoutError",
    "ModelNotFound",
    "ParseError",
    "QuotaError",
    "RateLimitError",
    "TransientError",
    "TruncatedError",
    "UnsupportedRequestError",
]
