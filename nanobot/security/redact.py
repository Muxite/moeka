"""Secret masking for log output. Dependency-free.

Modelled on hive's trace redactor. Order matters: the Authorization-header
pattern must run before the bare Bearer pattern, or the token after "Bearer"
survives in plaintext. Anything that logs request bodies, headers, URLs or tool
arguments must pass them through ``redact_text`` / ``redact_value``.
"""

from __future__ import annotations

import re
from typing import Any

_MASK = "<redacted>"

_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
# Name length is bounded on both sides so a long run of word characters cannot
# make the lazy prefix backtrack quadratically.
_NAME_VALUE_TEXT_RE = re.compile(
    r"(?i)\b([\w-]{0,64}?(?:key|token|secret|password|passwd|bearer)[\w-]{0,64})"
    r"(\s*[:=]\s*)(\S+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\bAuthorization\s*:\s*(?:(?:Bearer|Basic|Token)\s+)?\S+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_LONG_RUN_NEAR_KEYWORD_RE = re.compile(
    r"(?i)((?:key|token|secret|password|passwd|bearer)\b.{0,40}?)([A-Za-z0-9+/_=-]{32,})"
)


_HEAD_CHARS = 16 * 1024
_TAIL_CHARS = 4 * 1024


def redact_text(text: str | None) -> str | None:
    """Mask secrets in a string; ``None`` and non-strings pass through.

    Cost is bounded: a string longer than 20 KiB is reduced to its first 16 KiB
    and last 4 KiB (each redacted separately) joined by a
    ``...[N chars omitted]...`` marker. The omitted middle never reaches the
    output, so nothing can leak through it.
    """
    if text is None or not isinstance(text, str):
        return text
    if len(text) > _HEAD_CHARS + _TAIL_CHARS:
        omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
        return (
            _redact_bounded(text[:_HEAD_CHARS])
            + f"...[{omitted} chars omitted]..."
            + _redact_bounded(text[-_TAIL_CHARS:])
        )
    return _redact_bounded(text)


def _redact_bounded(text: str) -> str:
    masked = _SK_TOKEN_RE.sub(_MASK, text)
    masked = _AUTH_HEADER_RE.sub(f"Authorization: {_MASK}", masked)
    masked = _BEARER_RE.sub(f"Bearer {_MASK}", masked)
    masked = _NAME_VALUE_TEXT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", masked)
    masked = _LONG_RUN_NEAR_KEYWORD_RE.sub(lambda m: f"{m.group(1)}{_MASK}", masked)
    return masked


def redact_value(value: Any) -> Any:
    """Recursively mask secrets in strings inside dicts, lists and tuples."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value
