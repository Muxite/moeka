"""Typed LLM errors and the classifier that maps an error ``LLMResponse`` onto them.

Providers never raise for a failed call: they return an ``LLMResponse`` with
``finish_reason="error"`` and structured metadata (``error_status_code``,
``error_kind``, ``error_type``/``error_code``, retry-after). :func:`classify`
turns that into one :class:`LLMError` subclass, using the same retry/billing
predicates as the provider retry loop so the two never disagree. Free text is
only consulted where the providers themselves fall back to it (legacy
providers without structured metadata).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.providers.base import LLMResponse


class LLMError(Exception):
    """A model call failed. ``kind`` is a stable category; ``raw`` is the provider text.

    ``raw`` is diagnostic only: it is never returned to a caller as content.
    """

    kind: str = "unknown"
    default_retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        kind: str | None = None,
        call_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool | None = None,
        raw: str | None = None,
    ) -> None:
        if kind is not None:
            self.kind = kind
        self.call_id = call_id
        self.model = model
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.retryable = self.default_retryable if retryable is None else retryable
        self.raw = raw
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        parts = [f"{self.kind} error"]
        if self.provider or self.model:
            parts.append(f"from {self.provider or '?'}/{self.model or '?'}")
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        return " ".join(parts)


class AuthError(LLMError):
    """Credentials rejected (401/403 or an authentication error)."""

    kind = "auth"


class QuotaError(LLMError):
    """Out of credits / quota / billing (402, ``insufficient_quota``, ...). Not retryable."""

    kind = "quota"


class RateLimitError(LLMError):
    """Rate limited (429 that is not a quota error). Retryable, honouring ``retry_after``."""

    kind = "rate_limit"
    default_retryable = True


class TransientError(LLMError):
    """Server-side or connection failure (5xx, 408/409, connection reset). Retryable."""

    kind = "transient"
    default_retryable = True


class ContentFilterError(LLMError):
    """The provider refused or cut the output on a content-policy filter."""

    kind = "content_filter"


class LLMTimeoutError(LLMError, TimeoutError):
    """The request or stream timed out. Retryable; also a builtin ``TimeoutError``."""

    kind = "timeout"
    default_retryable = True


class ParseError(LLMError):
    """Structured output could not be parsed after ``attempts`` tries; ``raw`` is the text."""

    kind = "parse"

    def __init__(self, message: str | None = None, *, attempts: int = 1, **kwargs: object) -> None:
        self.attempts = attempts
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


class TruncatedError(LLMError):
    """The output hit the token limit (``finish_reason == "length"``) where a whole
    answer was required."""

    kind = "truncated"


class BudgetExceeded(LLMError):  # noqa: N818 - public contract name
    """A budget refused the call before it was sent."""

    kind = "budget"


class ModelNotFound(LLMError):  # noqa: N818 - public contract name
    """The provider does not know the requested model (404 / ``model_not_found``)."""

    kind = "model_not_found"


class UnsupportedRequestError(LLMError):
    """The request set fields the provider cannot honour and ``on_unsupported="raise"``.

    Raised before anything is sent; ``fields`` lists the unsupported names.
    """

    kind = "unsupported"

    def __init__(
        self,
        message: str | None = None,
        *,
        fields: Sequence[str] = (),
        **kwargs: object,
    ) -> None:
        self.fields = tuple(fields)
        if message is None and self.fields:
            message = (
                f"provider {kwargs.get('provider') or '?'} does not support "
                f"request fields: {', '.join(self.fields)}"
            )
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


_MODEL_NOT_FOUND_TOKENS = frozenset({"model_not_found", "not_found_error", "model_not_available"})
_AUTH_KINDS = frozenset({"authentication", "auth"})
_AUTH_TOKENS = frozenset({
    "authentication_error",
    "invalid_api_key",
    "permission_error",
    "unauthorized",
})
_INVALID_REQUEST_STATUSES = frozenset({400, 413, 422})


def _is_retryable(response: LLMResponse) -> bool:
    """``is_transient_response``, minus its free-text fallback when a status exists.

    A 4xx body that happens to mention "500" or "timeout" must not become
    retryable: with an HTTP status in hand the structured data decides.
    """
    from nanobot.providers.base import LLMProvider

    status = response.error_status_code
    if (
        status is not None
        and response.error_should_retry is None
        and status < 500
        and status not in LLMProvider._RETRYABLE_STATUS_CODES
        and (response.error_kind or "").strip().lower() not in LLMProvider._TRANSIENT_ERROR_KINDS
    ):
        return False
    return LLMProvider.is_transient_response(response)


def classify(
    response: LLMResponse,
    *,
    call_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> LLMError | None:
    """Map an error (or content-filtered) response to a typed :class:`LLMError`.

    Returns ``None`` for any other response. Order matters: billing wins over
    429 (a quota 429 must not be retried), and auth/model-not-found win over
    the generic transient check.
    """
    from nanobot.providers.base import LLMProvider

    common = {"call_id": call_id, "model": model, "provider": provider}
    status = response.error_status_code
    raw = response.content

    if response.finish_reason == "content_filter":
        return ContentFilterError(status=status, raw=raw, **common)
    if response.finish_reason != "error":
        return None

    retry_after = LLMProvider._extract_retry_after_from_response(response)
    retryable = _is_retryable(response)
    public_kind = LLMProvider.public_error_kind(response)
    kind = (response.error_kind or "").strip().lower()
    tokens = {
        token
        for token in (
            LLMProvider._normalize_error_token(response.error_type),
            LLMProvider._normalize_error_token(response.error_code),
        )
        if token is not None
    }
    base = {**common, "status": status, "raw": raw}

    if public_kind == "billing":
        return QuotaError(retryable=False, **base)
    if status in (401, 403) or kind in _AUTH_KINDS or tokens & _AUTH_TOKENS:
        return AuthError(retryable=False, **base)
    if tokens & _MODEL_NOT_FOUND_TOKENS or status == 404:
        return ModelNotFound(retryable=False, **base)
    if public_kind == "timeout":
        return LLMTimeoutError(retry_after=retry_after, retryable=True, **base)
    if public_kind == "rate_limit" or kind == "rate_limit":
        return RateLimitError(retry_after=retry_after, retryable=retryable, **base)
    if retryable or public_kind in ("server", "connection") or kind == "server_error":
        return TransientError(retry_after=retry_after, retryable=True, **base)
    if status in _INVALID_REQUEST_STATUSES:
        return LLMError(kind="invalid_request", retryable=False, **base)
    return LLMError(kind="unknown", retryable=False, **base)


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
    "classify",
]
