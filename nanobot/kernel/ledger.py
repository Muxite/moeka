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

Pricing key (Ruling J): a price belongs to a ``(provider, model)`` pair, never to a
model string alone. Two presets that share a model string under different providers
(e.g. a direct key and an aggregator) keep their own tier and price. A model-only
key is a wildcard for a pricing whose provider could not be resolved; conflicting
wildcards for one model are ambiguous and price nothing (``None``) rather than guess.
Collisions are logged and kept on :attr:`PricingTable.collisions`.

Usage source (Ruling J): every event carries ``usage_source``: ``"reported"`` (the
provider billed these tokens), ``"estimated"`` (the provider reported none and the
base class counted locally), ``"mixed"`` or ``"none"`` (no usage at all). A cost is
ground truth only when :attr:`LedgerEvent.cost_is_billed` is True; an estimated cost
is a hint, never a budget or routing fact.

Fail-open discipline: :class:`LedgerObserver` never raises into the provider call
(the same contract as :func:`nanobot.kernel.trace.safe_emit`); a broken sink, a
failing SQLite write or a bad pricing table is logged and swallowed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

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

UsageSource = Literal["reported", "estimated", "mixed", "none"]


def usage_source_of(usage: LLMUsage | None) -> UsageSource:
    """``"none"`` without usage, else the usage's own reported/estimated/mixed split."""
    if usage is None:
        return "none"
    return usage.source


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
    usage_source: UsageSource = "none"

    @property
    def cost_is_billed(self) -> bool:
        """True when ``cost_usd`` is ground truth: provider-reported usage at a known price.

        The ``tier="local"`` zero-cost convention does not depend on tokens, so a
        local ``0.0`` counts as billed whatever the usage source.
        """
        if self.cost_usd is None:
            return False
        if self.tier == "local" and self.cost_usd == 0.0:
            return True
        return self.usage_source == "reported"

    def to_trace(self) -> dict[str, Any]:
        return {"event": LEDGER_EVENT, **dataclasses.asdict(self)}


PricingKey = str | tuple[str, str]


class PricingTable:
    """``(provider, model)`` -> :class:`ModelPricing`, with collision detection.

    - A ``(provider, model)`` key prices only that provider's calls.
    - A bare ``model`` key is a wildcard (provider unknown when the table was built).
    - ``add`` keeps the first pricing for a key; a later, different pricing for the
      same key is a collision: logged, recorded on :attr:`collisions`, and for a
      wildcard it makes the model ambiguous (lookups return ``None``).
    - ``lookup`` tries the exact pair, then an unambiguous wildcard. A pair priced
      under a different provider is never borrowed.
    """

    def __init__(self, entries: Mapping[PricingKey, ModelPricing] | None = None) -> None:
        self._exact: dict[tuple[str, str], ModelPricing] = {}
        self._wild: dict[str, ModelPricing] = {}
        self._ambiguous: set[str] = set()
        self.collisions: list[tuple[PricingKey, ModelPricing, ModelPricing]] = []
        for key, pricing in (entries or {}).items():
            self.add(key, pricing)

    def add(self, key: PricingKey, pricing: ModelPricing) -> None:
        if isinstance(key, tuple):
            provider, model = key
            current = self._exact.get((provider, model))
            if current is None:
                self._exact[(provider, model)] = pricing
            elif current != pricing:
                self._collide(key, current, pricing)
            return
        current = self._wild.get(key)
        if current is None:
            self._wild[key] = pricing
        elif current != pricing:
            self._ambiguous.add(key)
            self._collide(key, current, pricing)

    def _collide(self, key: PricingKey, kept: ModelPricing, other: ModelPricing) -> None:
        self.collisions.append((key, kept, other))
        logger.warning(
            "ledger pricing collision for {!r}: kept {} and ignored {}{}",
            key, kept, other,
            "" if isinstance(key, tuple) else "; the model is ambiguous and priced as unknown",
        )

    def lookup(self, provider: str, model: str) -> ModelPricing | None:
        exact = self._exact.get((provider, model))
        if exact is not None:
            return exact
        if model in self._ambiguous:
            return None
        return self._wild.get(model)

    def __len__(self) -> int:
        return len(self._exact) + len(self._wild)


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

    *pricing* is a :class:`PricingTable` or a mapping it is built from
    (``(provider, model)`` or bare-model keys); an unpriced call gets ``tier=None``
    and ``cost_usd=None``. *store* is anything with ``record(LLMCallRecord)``
    (normally :class:`LLMUsageStore`).
    """

    def __init__(
        self,
        *,
        sink: TraceSink | None = None,
        store: _UsageRecorder | None = None,
        pricing: PricingTable | Mapping[PricingKey, ModelPricing] | None = None,
    ) -> None:
        self._sink = sink
        self._store = store
        if isinstance(pricing, PricingTable):
            self._pricing = pricing
        else:
            try:
                self._pricing = PricingTable(pricing)
            except Exception:  # noqa: BLE001 - a bad pricing table must not break calls
                logger.exception("ledger pricing table could not be built; pricing nothing")
                self._pricing = PricingTable()

    def _pricing_for(self, provider: str, model: str) -> ModelPricing | None:
        try:
            return self._pricing.lookup(provider, model)
        except Exception:  # noqa: BLE001 - a bad pricing table must not break the call
            logger.exception("ledger pricing lookup failed for {}", model)
            return None

    def enrich(self, record: LLMCallRecord) -> LLMCallRecord:
        """Return *record* with ``tier`` and ``cost_usd`` filled from the pricing."""
        pricing = self._pricing_for(record.provider, record.model)
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
            usage_source=usage_source_of(usage),
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
    "PricingKey",
    "PricingTable",
    "UsageSource",
    "compute_cost",
    "usage_source_of",
]
