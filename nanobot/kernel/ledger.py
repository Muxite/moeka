"""Per-call cost ledger (I6 measurement, design section 6).

Every physical LLM call becomes one ``model.call`` event on the host's
``TraceSink`` and one row in ``LLMUsageStore``: tokens, model tier, latency and
USD cost. The ledger only measures; it never gates, retries or reroutes.

Cost convention:
- Prices are USD per million tokens, taken from the model preset
  (``price_in_per_mtok`` / ``price_out_per_mtok`` / ``price_cache_read_per_mtok``).
- ``input_tokens`` includes cache reads and writes: the uncached share bills at the
  input price, cache reads at the cache-read price, cache writes at the input price.
- A missing price means the cost is unknown (``None``), never free.
- The one zero-cost convention: ``tier="local"`` with no prices set costs ``0.0``.

Fail-open discipline: :class:`LedgerObserver` never raises into the provider call
(the same contract as :func:`nanobot.kernel.trace.safe_emit`); a broken sink, a
failing SQLite write or a bad pricing table is logged and swallowed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanobot.kernel.trace import TraceSink, safe_emit
from nanobot.llm_usage.context import (
    LLMUsageSource,
    current_llm_usage_slot,
    current_llm_usage_trace_id,
)

if TYPE_CHECKING:
    from nanobot.llm_usage.models import LLMCallRecord
    from nanobot.providers.base import LLMUsage

LEDGER_EVENT = "model.call"
_PER_MTOK = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """The ledger-relevant slice of a model preset (USD per million tokens)."""

    tier: str | None = None
    price_in: float | None = None
    price_out: float | None = None
    price_cache_read: float | None = None


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One measured LLM call, content-free."""

    trace_id: str | None
    slot: str | None
    tier: str | None
    model: str
    provider: str
    tokens_in: int
    tokens_out: int
    tokens_cache_read: int
    latency_ms: float | None
    cost_usd: float | None
    source: LLMUsageSource

    def to_trace(self) -> dict[str, Any]:
        return {"event": LEDGER_EVENT, **dataclasses.asdict(self)}


class _UsageRecorder(Protocol):
    def record(self, call: LLMCallRecord) -> None: ...


def compute_cost(
    usage: LLMUsage | None,
    price_in: float | None,
    price_out: float | None,
    price_cache_read: float | None,
    *,
    tier: str | None = None,
) -> float | None:
    """USD cost of one call, or ``None`` when it cannot be known.

    ``tier="local"`` with no price at all is the documented zero-cost convention;
    any other missing price needed by the usage yields ``None`` (unknown, not 0).
    """
    if tier == "local" and price_in is None and price_out is None and price_cache_read is None:
        return 0.0
    if usage is None or price_in is None or price_out is None:
        return None
    cache_read = usage.cache_read_tokens or 0
    if cache_read and price_cache_read is None:
        return None
    uncached_in = max(0, usage.input_tokens - cache_read)
    cost = uncached_in * price_in + usage.output_tokens * price_out
    if cache_read:
        cost += cache_read * (price_cache_read or 0.0)
    return cost / _PER_MTOK


class LedgerObserver:
    """An ``LLMCallObserver`` that turns each call record into a ledger entry.

    *pricing* maps a model name to its :class:`ModelPricing`; an unknown model
    gets ``tier=None`` and ``cost_usd=None``. *store* is anything with
    ``record(LLMCallRecord)`` (normally :class:`LLMUsageStore`).
    """

    def __init__(
        self,
        *,
        sink: TraceSink | None = None,
        store: _UsageRecorder | None = None,
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self._sink = sink
        self._store = store
        self._pricing: Mapping[str, ModelPricing] = pricing or {}

    def _pricing_for(self, model: str) -> ModelPricing | None:
        try:
            return self._pricing.get(model)
        except Exception:  # noqa: BLE001 - a bad pricing table must not break the call
            logger.exception("ledger pricing lookup failed for {}", model)
            return None

    def enrich(self, record: LLMCallRecord) -> LLMCallRecord:
        """Return *record* with ``tier`` and ``cost_usd`` filled from the pricing."""
        pricing = self._pricing_for(record.model)
        if pricing is None:
            return record
        cost = compute_cost(
            record.usage,
            pricing.price_in,
            pricing.price_out,
            pricing.price_cache_read,
            tier=pricing.tier,
        )
        return dataclasses.replace(record, tier=pricing.tier, cost_usd=cost)

    @staticmethod
    def _event(record: LLMCallRecord) -> LedgerEvent:
        usage = record.usage
        return LedgerEvent(
            trace_id=current_llm_usage_trace_id(),
            slot=current_llm_usage_slot(),
            tier=record.tier,
            model=record.model,
            provider=record.provider,
            tokens_in=usage.input_tokens if usage is not None else 0,
            tokens_out=usage.output_tokens if usage is not None else 0,
            tokens_cache_read=(usage.cache_read_tokens or 0) if usage is not None else 0,
            latency_ms=float(record.duration_ms),
            cost_usd=record.cost_usd,
            source=record.source,
        )

    def event_for(self, record: LLMCallRecord) -> LedgerEvent:
        """The ledger event for *record* (slot and trace id from the context vars)."""
        return self._event(self.enrich(record))

    def __call__(self, record: LLMCallRecord) -> None:
        try:
            enriched = self.enrich(record)
        except Exception:  # noqa: BLE001
            logger.exception("ledger enrichment failed for {}", record.model)
            enriched = record
        if self._sink is not None:
            try:
                safe_emit(self._sink, self._event(enriched).to_trace())
            except Exception:  # noqa: BLE001
                logger.exception("ledger event build failed for {}", record.model)
        if self._store is not None:
            try:
                self._store.record(enriched)
            except Exception:  # noqa: BLE001 - SQLite errors must not fail the call
                logger.exception("ledger store write failed for {}", record.model)


__all__ = [
    "LEDGER_EVENT",
    "LedgerEvent",
    "LedgerObserver",
    "ModelPricing",
    "compute_cost",
]
