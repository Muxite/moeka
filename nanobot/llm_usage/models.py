"""Content-free records emitted for physical LLM provider calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nanobot.llm_usage.context import LLMUsageSource
from nanobot.providers.base import LLMUsage

_KEY_FP_RE = re.compile(r"[0-9a-f]{12}")
_INSTANCE_UUID_RE = re.compile(r"[0-9a-f]{32}")


def _valid_key_fp(value: object) -> bool:
    return isinstance(value, str) and _KEY_FP_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """The small, chart-oriented result of one provider call attempt.

    Request messages, response text, reasoning, and tool payloads deliberately do
    not belong to this contract. Sessions already own that content.
    """

    started_at_ms: int
    duration_ms: int
    provider: str
    model: str
    source: LLMUsageSource
    stream: bool
    finish_reason: str
    usage: LLMUsage | None = None
    error_status_code: int | None = None
    error_kind: str | None = None
    # Ledger enrichment (Task 14): set by the kernel ledger, never by providers.
    tier: str | None = None
    cost_usd: float | None = None
    # Fingerprint of the API key that paid (spec 009): 12 hex chars of sha256, never the key.
    key_fp: str | None = None
    # Routing provenance. ``model``/``provider`` remain the actual physical attempt.
    requested_preset: str | None = None
    fallback_reason: str | None = None
    instance_uuid: str | None = None

    @property
    def usage_source(self) -> Literal["reported", "estimated", "mixed", "none"]:
        """Whether ``usage`` was billed by the provider or estimated locally (Ruling J).

        ``"none"`` when the call carried no usage at all. Derived, not stored: the
        store keeps ``reported_tokens`` / ``estimated_tokens``, which encode the same.
        """
        if self.usage is None:
            return "none"
        return self.usage.source

    def __post_init__(self) -> None:
        if self.started_at_ms < 0 or self.duration_ms < 0:
            raise ValueError("LLM usage timestamps must be non-negative")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("LLM usage provider and model must be non-empty")
        if self.source not in {"user", "api", "cron", "dream", "system"}:
            raise ValueError("invalid LLM usage source")
        if not self.finish_reason.strip():
            raise ValueError("LLM usage finish_reason must be non-empty")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("LLM usage cost_usd must be non-negative")
        for name, value, maximum in (
            ("requested_preset", self.requested_preset, 120),
            ("fallback_reason", self.fallback_reason, 80),
            ("instance_uuid", self.instance_uuid, 32),
        ):
            if value is not None and (not value.strip() or len(value) > maximum):
                raise ValueError(f"LLM usage {name} must be non-empty and at most {maximum} characters")
        if self.instance_uuid is not None and not _INSTANCE_UUID_RE.fullmatch(self.instance_uuid):
            raise ValueError("LLM usage instance_uuid must be 32 lowercase hex characters")
        if self.key_fp is not None and not _valid_key_fp(self.key_fp):
            # Never echo the value: a key passed here by mistake must not reach a log.
            raise ValueError("LLM usage key_fp must be 12 lowercase hex characters")
