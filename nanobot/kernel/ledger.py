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

Call attribution: the kernel LLM layer binds a :class:`CallAttribution` (call id,
model alias, host tags) in a context var around each logical call, so every physical
attempt's event carries them plus its attempt number. Calls made outside the LLM
layer (the agent loop) have no attribution and leave those fields empty.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from loguru import logger

from nanobot.kernel.frozen import FrozenMap, thaw
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

# Version of the usage-record schema these events speak (``schemas/usage-record.v1``).
# Additive fields bump the minor; a removed or re-meant field bumps the major.
SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("1.0",)
UNATTRIBUTED = "unattributed"

UsageSource = Literal["reported", "estimated", "mixed", "none"]


def usage_source_of(usage: LLMUsage | None) -> UsageSource:
    """``"none"`` without usage, else the usage's own reported/estimated/mixed split."""
    if usage is None:
        return "none"
    return usage.source


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who a call is for (typed, never inferred from ``tags``).

    ``consumer``: the product on whose behalf the call runs; ``agent``: the agent or
    pipeline actor; ``session``: run, build or chat id; ``role``: functional role
    (maker, checker, ...); ``purpose``: task type or stage. All optional here; the
    ledger fills ``consumer`` with :data:`UNATTRIBUTED` when no layer set one.
    """

    consumer: str | None = None
    agent: str | None = None
    session: str | None = None
    role: str | None = None
    purpose: str | None = None

    def over(self, base: Attribution | None) -> Attribution:
        """This attribution with *base* filling every field left ``None``."""
        if base is None:
            return self
        return Attribution(
            consumer=self.consumer if self.consumer is not None else base.consumer,
            agent=self.agent if self.agent is not None else base.agent,
            session=self.session if self.session is not None else base.session,
            role=self.role if self.role is not None else base.role,
            purpose=self.purpose if self.purpose is not None else base.purpose,
        )

    def to_dict(self) -> dict[str, str | None]:
        return dataclasses.asdict(self)


_CURRENT_ATTRIBUTION: ContextVar[Attribution | None] = ContextVar(
    "moeka_usage_attribution", default=None,
)


def current_attribution() -> Attribution | None:
    """The ambient :class:`Attribution` bound by :func:`bind_attribution` (or ``None``)."""
    return _CURRENT_ATTRIBUTION.get()


@contextmanager
def bind_attribution(attribution: Attribution) -> Generator[Attribution]:
    """Attribute nested model calls to *attribution*; unset fields keep the outer value."""
    merged = attribution.over(_CURRENT_ATTRIBUTION.get())
    token = _CURRENT_ATTRIBUTION.set(merged)
    try:
        yield merged
    finally:
        _CURRENT_ATTRIBUTION.reset(token)


@dataclass(slots=True)
class CallAttribution:
    """Who one logical LLM call belongs to; bound by the kernel LLM layer.

    Mutable on purpose: the observer counts physical attempts into it and leaves
    the last attempt's cost, so the caller can read them back after the call.
    ``on_event`` (budget settlement) receives every physical attempt's
    :class:`LedgerEvent`; it is called fail-open.
    """

    call_id: str
    alias: str | None = None
    tags: Mapping[str, Any] = field(default_factory=FrozenMap)
    cached: bool = False
    attempts: int = 0
    cost_usd: float | None = None
    on_event: Callable[[LedgerEvent], None] | None = None
    attribution: Attribution | None = None
    request_key: str | None = None
    prompt_version: str | None = None
    parent_call_id: str | None = None


_CURRENT_CALL: ContextVar[CallAttribution | None] = ContextVar(
    "moeka_llm_call_attribution", default=None,
)


def current_call_attribution() -> CallAttribution | None:
    return _CURRENT_CALL.get()


@contextmanager
def call_attribution(attribution: CallAttribution) -> Generator[CallAttribution]:
    """Attribute nested provider calls (in this task) to *attribution*."""
    token = _CURRENT_CALL.set(attribution)
    try:
        yield attribution
    finally:
        _CURRENT_CALL.reset(token)


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
    finish_reason: str | None = None
    # Kernel LLM-layer attribution; ``None`` / empty for calls made outside it.
    call_id: str | None = None
    alias: str | None = None
    attempt: int | None = None
    cached: bool = False
    tags: Mapping[str, Any] = field(default_factory=FrozenMap)
    # usage-record.v1 additions (all optional, so older call sites keep working).
    consumer: str = UNATTRIBUTED
    agent: str | None = None
    session: str | None = None
    role: str | None = None
    purpose: str | None = None
    tokens_cache_write: int | None = None
    tokens_reasoning: int | None = None
    price_source: str = "none"
    outcome: str = "ok"
    error_kind: str | None = None
    started_at_ms: int | None = None
    request_key: str | None = None
    prompt_version: str | None = None
    parent_call_id: str | None = None
    waste_label: str | None = None
    waste_set_by: str | None = None

    @property
    def record_id(self) -> str:
        return f"{self.call_id or 'direct'}:{self.attempt or 1}"

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
        """The ``model.call`` event: a ``usage-record.v1`` document plus ``event``.

        ``cached`` is the pre-schema name of ``cache_hit``; kept as a deprecated alias.
        """
        fields = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        fields["tags"] = thaw(self.tags)
        return {
            "event": LEDGER_EVENT,
            "schema_version": SCHEMA_VERSION,
            "record_id": self.record_id,
            "kind": "model_call",
            "cache_hit": False,
            "cost_billed": self.cost_is_billed,
            "producer": {"name": "moeka", "version": None},
            **fields,
        }


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

    def pricing_for(self, provider: str, model: str) -> ModelPricing | None:
        """The pricing this observer applies to ``(provider, model)`` (``None`` = unpriced)."""
        return self._pricing_for(provider, model)

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
    def _outcome(record: LLMCallRecord) -> str:
        if record.error_kind == "timeout":
            return "timeout"
        reason = (record.finish_reason or "").strip().lower()
        if reason == "cancelled":
            return "cancelled"
        if reason == "error" or record.error_kind is not None:
            return "error"
        if reason == "length":
            return "truncated"
        return "ok"

    @staticmethod
    def _price_source(record: LLMCallRecord) -> str:
        if record.cost_usd is None:
            return "none"
        if record.tier == "local" and record.cost_usd == 0.0:
            return "local_zero"
        return "price_table"

    @staticmethod
    def _event(
        record: LLMCallRecord, *, attempt: int | None = None, direct_ids: bool = False,
    ) -> LedgerEvent:
        usage = record.usage
        call = current_call_attribution()
        if call is not None and attempt is None:
            attempt = call.attempts or None
        who = (call.attribution if call is not None else None) or Attribution()
        who = who.over(current_attribution())
        call_id = call.call_id if call is not None else None
        if direct_ids and call is None:
            # A call made outside the LLM layer (agent loop): give it identity (G11).
            call_id, attempt = f"direct-{uuid.uuid4().hex[:12]}", 1
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
            finish_reason=record.finish_reason,
            call_id=call_id,
            alias=call.alias if call is not None else None,
            attempt=attempt,
            cached=call.cached if call is not None else False,
            tags=call.tags if call is not None else FrozenMap(),
            consumer=who.consumer or UNATTRIBUTED,
            agent=who.agent,
            session=who.session,
            role=who.role,
            purpose=who.purpose,
            tokens_cache_write=usage.cache_write_tokens if usage is not None else None,
            price_source=LedgerObserver._price_source(record),
            outcome=LedgerObserver._outcome(record),
            error_kind=record.error_kind,
            started_at_ms=record.started_at_ms,
            request_key=call.request_key if call is not None else None,
            prompt_version=call.prompt_version if call is not None else None,
            parent_call_id=call.parent_call_id if call is not None else None,
            waste_label="retry" if (attempt or 0) > 1 else None,
            waste_set_by="kernel" if (attempt or 0) > 1 else None,
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
        call = current_call_attribution()
        attempt: int | None = None
        if call is not None:
            call.attempts += 1
            call.cost_usd = enriched.cost_usd
            attempt = call.attempts
        on_event = call.on_event if call is not None else None
        event: LedgerEvent | None = None
        try:
            event = self._event(enriched, attempt=attempt, direct_ids=True)
        except Exception:  # noqa: BLE001
            logger.exception("ledger event build failed for {}", record.model)
        if event is not None:
            if self._sink is not None:
                safe_emit(self._sink, event.to_trace())
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - settlement must not fail the call
                    logger.exception("ledger on_event hook failed for {}", record.model)
        if self._store is not None:
            try:
                record_event = getattr(self._store, "record_event", None)
                if record_event is not None and event is not None:
                    record_event(enriched, event)
                else:
                    self._store.record(enriched)
            except Exception:  # noqa: BLE001 - SQLite errors must not fail the call
                logger.exception("ledger store write failed for {}", record.model)


__all__ = [
    "LEDGER_EVENT",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "UNATTRIBUTED",
    "Attribution",
    "CallAttribution",
    "LedgerEvent",
    "LedgerObserver",
    "ModelPricing",
    "PricingKey",
    "PricingTable",
    "UsageSource",
    "bind_attribution",
    "call_attribution",
    "compute_cost",
    "current_attribution",
    "current_call_attribution",
    "usage_source_of",
]
