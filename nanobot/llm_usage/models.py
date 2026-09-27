"""Content-free records emitted for physical LLM provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nanobot.llm_usage.context import LLMUsageSource
from nanobot.providers.base import LLMUsage


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
