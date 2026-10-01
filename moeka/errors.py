"""Typed errors raised by model calls (``kind`` is the stable category), plus the
instance-lock errors of ``Kernel(env, attach=...)`` (spec 005) and ``VariantError``
(a variant override that does not fit an agent's tools, spec 006)."""

from nanobot.kernel.instance_lock import InstanceLockedError, ReadOnlyKernelError
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
from nanobot.kernel.variants import VariantError

__all__ = [
    "AuthError",
    "BudgetExceeded",
    "ContentFilterError",
    "InstanceLockedError",
    "LLMError",
    "LLMTimeoutError",
    "ModelNotFound",
    "ParseError",
    "QuotaError",
    "RateLimitError",
    "ReadOnlyKernelError",
    "TransientError",
    "TruncatedError",
    "UnsupportedRequestError",
    "VariantError",
]
