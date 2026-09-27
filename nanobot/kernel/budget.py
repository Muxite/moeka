"""Budget and response-cache hooks for the kernel LLM layer.

The host owns prices and dollar policy; the kernel only asks. Before a logical
model call is sent, the kernel builds a :class:`CallEstimate` (the worst case the
call can cost) and asks the host's :class:`Budget` to ``admit`` it. The budget
returns an opaque reservation or raises :class:`BudgetExceeded` to refuse; a
refused call never reaches a provider. Every physical attempt's ledger event
(``model.call``) is then ``settle``-d against that reservation, and when the
logical call ends the unused part is ``release``-d.

Admission happens once per logical call:

- The LLM layer (``generate`` / ``complete`` / ``complete_json`` / ``stream`` /
  ``batch``) admits before its first provider call. A ``complete_json`` call is
  one admission covering every round (``1 + retries``, plus a native
  ``response_format`` round that the provider may reject).
- :class:`BudgetedProvider` wraps every provider the kernel hands out and admits
  only calls that did not come through the LLM layer (no active
  :class:`~nanobot.kernel.ledger.CallAttribution`), i.e. an agent loop calling a
  provider directly. LLM-layer calls pass straight through, so nothing is
  admitted twice.

Settlement is per physical ledger event: a failover through ``FallbackProvider``
is settled once per attempt it actually made, never once more for the wrapper.

A :class:`ResponseCache` is consulted (before admission) for calls whose
``GenerateOptions.cache`` is True; only successful, non-truncated completions are
stored. :class:`CapBudget` is the reference budget.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

from nanobot.kernel.frozen import FrozenMap, thaw
from nanobot.kernel.ledger import (
    CallAttribution,
    ModelPricing,
    call_attribution,
    current_call_attribution,
)
from nanobot.kernel.llm_errors import BudgetExceeded
from nanobot.kernel.trace import TraceSink, safe_emit
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse

if TYPE_CHECKING:
    from nanobot.kernel.ledger import LedgerEvent
    from nanobot.kernel.llm import Completion
    from nanobot.kernel.sampling import Sampling

_PER_MTOK = 1_000_000

# A flat per-image input-token charge for estimates. Provider image costs vary
# with resolution and detail (roughly 85-1600 tokens on OpenAI, ~1600 for a
# 1092px image on Anthropic); the estimate takes the high end so it stays a
# worst case for typical inputs.
IMAGE_TOKENS = 1600

# Extra input tokens per JSON re-prompt round beyond the previous reply itself
# (the correction instructions plus the parse error text).
REPROMPT_OVERHEAD_TOKENS = 256

_IMAGE_PART_TYPES = frozenset({"image_url", "image", "input_image"})


# -- the estimate -----------------------------------------------------------------


@dataclass(frozen=True)
class CallEstimate:
    """The worst case one logical call can cost, built before it is sent.

    - ``prompt_tokens``: estimated input tokens of one round (images at
      :data:`IMAGE_TOKENS` each; a JSON schema is counted once).
    - ``max_output_tokens``: the resolved output cap of one round.
    - ``rounds``: request rounds the call may make (``complete_json``:
      ``1 + retries``, plus one when a native ``response_format`` round could be
      rejected).
    - ``worst_case_tokens``: input plus output tokens over all rounds.
    - ``worst_case_usd``: that token count priced, or ``None`` when the model has
      no known price (``tier="local"`` without prices is ``0.0``).
    - ``tags``: the call's host tags.
    """

    call_id: str
    alias: str | None
    model: str
    provider: str
    prompt_tokens: int
    max_output_tokens: int
    rounds: int
    worst_case_tokens: int
    worst_case_usd: float | None
    tags: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if not isinstance(self.tags, FrozenMap):
            object.__setattr__(self, "tags", FrozenMap(self.tags or {}))

    def to_trace(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "alias": self.alias,
            "model": self.model,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "rounds": self.rounds,
            "worst_case_tokens": self.worst_case_tokens,
            "worst_case_usd": self.worst_case_usd,
            "tags": thaw(self.tags),
        }


def count_images(messages: Sequence[Mapping[str, Any]]) -> int:
    """Image content parts across *messages* (OpenAI / Anthropic / Responses shapes)."""
    count = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, (list, tuple)):
            for part in content:
                if isinstance(part, Mapping) and part.get("type") in _IMAGE_PART_TYPES:
                    count += 1
    return count


def prompt_tokens_of(
    messages: Sequence[Mapping[str, Any]], tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimated input tokens for one request: text via ``estimate_prompt_tokens``
    plus :data:`IMAGE_TOKENS` per image part."""
    from nanobot.utils.helpers import estimate_prompt_tokens

    plain = [dict(message) for message in messages]
    return estimate_prompt_tokens(plain, tools) + IMAGE_TOKENS * count_images(messages)


def price_tokens(
    pricing: ModelPricing | None, input_tokens: int, output_tokens: int,
) -> float | None:
    """USD for the token counts, ``None`` when unpriced (local tier without prices = 0)."""
    if pricing is None:
        return None
    if pricing.tier == "local" and pricing.price_in is None and pricing.price_out is None:
        return 0.0
    if pricing.price_in is None or pricing.price_out is None:
        return None
    return (input_tokens * pricing.price_in + output_tokens * pricing.price_out) / _PER_MTOK


def build_estimate(
    *,
    call_id: str,
    alias: str | None,
    model: str,
    provider: str,
    prompt_tokens: int,
    max_output_tokens: int,
    rounds: int = 1,
    reprompts: int = 0,
    pricing: ModelPricing | None,
    tags: Mapping[str, Any] | None = None,
) -> CallEstimate:
    """A :class:`CallEstimate` for *rounds* rounds of one prompt.

    Each of the *reprompts* rounds also re-sends the previous reply (up to
    ``max_output_tokens``) plus :data:`REPROMPT_OVERHEAD_TOKENS` of instructions.
    """
    rounds = max(1, rounds)
    input_tokens = rounds * prompt_tokens + max(0, reprompts) * (
        max_output_tokens + REPROMPT_OVERHEAD_TOKENS
    )
    output_tokens = rounds * max_output_tokens
    return CallEstimate(
        call_id=call_id,
        alias=alias,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        max_output_tokens=max_output_tokens,
        rounds=rounds,
        worst_case_tokens=input_tokens + output_tokens,
        worst_case_usd=price_tokens(pricing, input_tokens, output_tokens),
        tags=FrozenMap(tags or {}),
    )


# -- the host hooks -----------------------------------------------------------------


@runtime_checkable
class Budget(Protocol):
    """Host spend policy. The kernel calls it on its loop thread; it may be shared.

    - ``admit(estimate)`` returns an opaque reservation, or raises
      :class:`BudgetExceeded` to refuse the call (nothing is sent).
    - ``settle(reservation, event)`` is called once per physical attempt's
      ``model.call`` ledger event (``event.cost_usd`` may be ``None``).
    - ``release(reservation)`` is called once when the logical call ends
      (success or failure): free whatever was reserved but not settled.
    """

    def admit(self, estimate: CallEstimate) -> Any: ...

    def settle(self, reservation: Any, event: LedgerEvent) -> None: ...

    def release(self, reservation: Any) -> None: ...


@runtime_checkable
class ResponseCache(Protocol):
    """A host-owned response cache keyed by :meth:`LLM.request_key`."""

    def get(self, key: str) -> Completion | None: ...

    def put(self, key: str, completion: Completion) -> None: ...


# -- the reference budget -------------------------------------------------------------


def _tag_slot(key: str, value: Any) -> tuple[str, Hashable]:
    try:
        hash(value)
    except TypeError:
        return key, repr(value)
    return key, value


@dataclass(eq=False)
class _CapReservation:
    call_id: str
    usd: float
    tokens: int
    slots: tuple[tuple[str, Hashable], ...]
    unpriced_usage: bool = False
    released: bool = False


class CapBudget:
    """Reference :class:`Budget`: hard caps on spend plus outstanding reservations.

    - ``limit_usd``: total USD cap; ``limit_tokens``: total token cap.
    - ``per_tag``: ``{tag_key: usd_limit}`` caps every distinct value of that tag
      separately (e.g. ``{"stage": 0.5}`` gives each stage 50 cents). Calls
      without the tag are not counted under it.
    - A call is admitted only if ``spent + reserved + worst case`` stays within
      every applicable cap. Under a USD cap an estimate without a price
      (``worst_case_usd is None``) is refused unless ``allow_unpriced``.
    - Settled ``cost_usd`` turns the reservation into spend. An attempt that used
      tokens but has no price is not free: when the call ends, what is left of
      its USD reservation is charged instead of released.

    Thread-safe; one instance may be shared by several kernels.
    """

    def __init__(
        self,
        limit_usd: float | None = None,
        limit_tokens: int | None = None,
        per_tag: Mapping[str, float] | None = None,
        allow_unpriced: bool = False,
    ) -> None:
        self.limit_usd = limit_usd
        self.limit_tokens = limit_tokens
        self.per_tag: Mapping[str, float] = dict(per_tag or {})
        self.allow_unpriced = allow_unpriced
        self._lock = threading.Lock()
        self._spent_usd = 0.0
        self._spent_tokens = 0
        self._reserved_usd = 0.0
        self._reserved_tokens = 0
        self._tag_spent: dict[tuple[str, Hashable], float] = {}
        self._tag_reserved: dict[tuple[str, Hashable], float] = {}

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def spent_tokens(self) -> int:
        return self._spent_tokens

    @property
    def reserved_usd(self) -> float:
        return self._reserved_usd

    @property
    def reserved_tokens(self) -> int:
        return self._reserved_tokens

    def exposure(self, tag: str | None = None, value: Any = None) -> float:
        """Spent plus reserved USD: in total, or for one ``(tag, value)``."""
        with self._lock:
            if tag is None:
                return self._spent_usd + self._reserved_usd
            slot = _tag_slot(tag, value)
            return self._tag_spent.get(slot, 0.0) + self._tag_reserved.get(slot, 0.0)

    def _refuse(self, estimate: CallEstimate, reason: str) -> BudgetExceeded:
        return BudgetExceeded(
            f"budget refused call: {reason}",
            call_id=estimate.call_id, model=estimate.model, provider=estimate.provider,
        )

    def admit(self, estimate: CallEstimate) -> _CapReservation:
        slots = tuple(
            _tag_slot(key, estimate.tags[key]) for key in self.per_tag if key in estimate.tags
        )
        usd = estimate.worst_case_usd
        tokens = estimate.worst_case_tokens
        with self._lock:
            if usd is None and (self.limit_usd is not None or slots):
                if not self.allow_unpriced:
                    raise self._refuse(estimate, "the model has no price under a USD cap")
            amount = usd or 0.0
            if self.limit_usd is not None:
                exposure = self._spent_usd + self._reserved_usd
                if exposure + amount > self.limit_usd:
                    raise self._refuse(
                        estimate,
                        f"worst case ${amount:.6f} on top of ${exposure:.6f} exceeds "
                        f"limit_usd=${self.limit_usd:.6f}",
                    )
            if self.limit_tokens is not None:
                used = self._spent_tokens + self._reserved_tokens
                if used + tokens > self.limit_tokens:
                    raise self._refuse(
                        estimate,
                        f"worst case {tokens} tokens on top of {used} exceeds "
                        f"limit_tokens={self.limit_tokens}",
                    )
            for slot in slots:
                limit = self.per_tag[slot[0]]
                exposure = self._tag_spent.get(slot, 0.0) + self._tag_reserved.get(slot, 0.0)
                if exposure + amount > limit:
                    raise self._refuse(
                        estimate,
                        f"worst case ${amount:.6f} on top of ${exposure:.6f} exceeds the "
                        f"{slot[0]}={slot[1]!r} cap ${limit:.6f}",
                    )
            self._reserved_usd += amount
            self._reserved_tokens += tokens
            for slot in slots:
                self._tag_reserved[slot] = self._tag_reserved.get(slot, 0.0) + amount
            return _CapReservation(estimate.call_id, amount, tokens, slots)

    def _consume(self, reservation: _CapReservation, usd: float, tokens: int) -> None:
        """Move up to *usd*/*tokens* of *reservation* out of the reserved totals."""
        take_usd = min(reservation.usd, usd)
        take_tokens = min(reservation.tokens, tokens)
        reservation.usd -= take_usd
        reservation.tokens -= take_tokens
        self._reserved_usd = max(0.0, self._reserved_usd - take_usd)
        self._reserved_tokens = max(0, self._reserved_tokens - take_tokens)
        for slot in reservation.slots:
            self._tag_reserved[slot] = max(0.0, self._tag_reserved.get(slot, 0.0) - take_usd)

    def settle(self, reservation: _CapReservation, event: LedgerEvent) -> None:
        tokens = event.tokens_in + event.tokens_out
        cost = event.cost_usd
        with self._lock:
            self._spent_tokens += tokens
            if cost is None:
                if tokens:
                    reservation.unpriced_usage = True
                if not reservation.released:
                    self._consume(reservation, 0.0, tokens)
                return
            self._spent_usd += cost
            for slot in reservation.slots:
                self._tag_spent[slot] = self._tag_spent.get(slot, 0.0) + cost
            if not reservation.released:
                self._consume(reservation, cost, tokens)

    def release(self, reservation: _CapReservation) -> None:
        with self._lock:
            if reservation.released:
                return
            reservation.released = True
            if reservation.unpriced_usage and reservation.usd:
                # Tokens were billed at an unknown price: keep the reservation as spend.
                self._spent_usd += reservation.usd
                for slot in reservation.slots:
                    self._tag_spent[slot] = self._tag_spent.get(slot, 0.0) + reservation.usd
            self._consume(reservation, reservation.usd, reservation.tokens)

    def __repr__(self) -> str:
        return (
            f"CapBudget(limit_usd={self.limit_usd}, limit_tokens={self.limit_tokens}, "
            f"per_tag={dict(self.per_tag)}, spent_usd={self._spent_usd:.6f}, "
            f"reserved_usd={self._reserved_usd:.6f})"
        )


# -- kernel-side metering ---------------------------------------------------------------


class Admission:
    """One admitted logical call: settles its ledger events, then releases once."""

    __slots__ = ("_budget", "_estimate", "_reservation", "_released")

    def __init__(self, budget: Budget, estimate: CallEstimate, reservation: Any) -> None:
        self._budget = budget
        self._estimate = estimate
        self._reservation = reservation
        self._released = False

    @property
    def estimate(self) -> CallEstimate:
        return self._estimate

    def settle(self, event: LedgerEvent) -> None:
        """Settle one physical attempt (ignores events of other calls). Never raises."""
        if event.call_id != self._estimate.call_id:
            return
        try:
            self._budget.settle(self._reservation, event)
        except Exception:  # noqa: BLE001 - a broken budget must not fail the call
            logger.exception("budget settle failed for call {}", event.call_id)

    def release(self) -> None:
        """Release the unused reservation. Idempotent; never raises."""
        if self._released:
            return
        self._released = True
        try:
            self._budget.release(self._reservation)
        except Exception:  # noqa: BLE001
            logger.exception("budget release failed for call {}", self._estimate.call_id)


class Metering:
    """Admission against the kernel's :class:`Budget`, with ``budget.*`` trace events."""

    def __init__(self, budget: Budget, sink: TraceSink) -> None:
        self.budget = budget
        self._sink = sink

    def admit(self, estimate: CallEstimate) -> Admission:
        """Admit *estimate* or raise :class:`BudgetExceeded` (``budget.refuse`` emitted)."""
        try:
            reservation = self.budget.admit(estimate)
        except BudgetExceeded as exc:
            if exc.call_id is None:
                exc.call_id = estimate.call_id
            exc.model = exc.model or estimate.model
            exc.provider = exc.provider or estimate.provider
            safe_emit(self._sink, {
                "event": "budget.refuse", **estimate.to_trace(), "reason": str(exc),
            })
            raise
        safe_emit(self._sink, {"event": "budget.admit", **estimate.to_trace()})
        return Admission(self.budget, estimate, reservation)


# -- the provider wrapper ------------------------------------------------------------------

Estimator = Callable[[dict[str, Any]], CallEstimate]


def _call_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """``messages`` / ``tools`` / ``model`` / ``max_tokens`` of a provider call."""
    merged = dict(kwargs)
    for index, name in enumerate(("messages", "tools", "model", "max_tokens")):
        if index < len(args) and name not in merged:
            merged[name] = args[index]
    return merged


class BudgetedProvider(LLMProvider):
    """Meter direct provider calls against the kernel budget.

    Wraps a pool or host-injected provider (modelled on ``FallbackProvider``'s
    delegation). Calls made through the kernel LLM layer carry a
    :class:`~nanobot.kernel.ledger.CallAttribution` and were already admitted:
    they pass straight through. Any other call (an agent loop using the provider
    directly) is estimated with *estimator*, admitted (a refusal raises
    :class:`BudgetExceeded` before anything is sent), attributed so its ledger
    events settle against the reservation, and released when it returns.
    """

    def __init__(
        self,
        inner: LLMProvider,
        metering: Metering,
        estimator: Estimator,
        *,
        alias: str | None = None,
    ) -> None:
        self._inner = inner
        self._metering = metering
        self._estimator = estimator
        self._alias = alias
        generation = inner.generation
        super().__init__(inner.api_key, inner.api_base, provider_name=inner.provider_name)
        inner.generation = generation

    @property
    def inner(self) -> LLMProvider:
        return self._inner

    # -- delegated state ----------------------------------------------------------

    @property
    def generation(self) -> GenerationSettings:  # type: ignore[override]
        return self._inner.generation

    @generation.setter
    def generation(self, value: GenerationSettings) -> None:
        self._inner.generation = value

    @property
    def trace_sink(self) -> TraceSink | None:  # type: ignore[override]
        return self._inner.trace_sink

    @trace_sink.setter
    def trace_sink(self, value: TraceSink | None) -> None:
        self._inner.trace_sink = value

    @property
    def supported_sampling_fields(self) -> frozenset[str]:  # type: ignore[override]
        return self._inner.supported_sampling_fields

    @supported_sampling_fields.setter
    def supported_sampling_fields(self, value: frozenset[str]) -> None:
        self._inner.supported_sampling_fields = value

    @property
    def stream_idle_timeout_s(self) -> float:  # type: ignore[override]
        return self._inner.stream_idle_timeout_s

    @stream_idle_timeout_s.setter
    def stream_idle_timeout_s(self, value: float) -> None:
        self._inner.stream_idle_timeout_s = value

    @property
    def supports_stream_recover_callback(self) -> bool:
        return bool(getattr(self._inner, "supports_stream_recover_callback", False))

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes the wrapper does not define itself.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def get_default_model(self) -> str:
        return self._inner.get_default_model()

    def set_llm_call_observer(self, observer: Any) -> None:
        self._inner.set_llm_call_observer(observer)

    @property
    def _llm_call_observer(self) -> Any:  # type: ignore[override]
        return getattr(self._inner, "_llm_call_observer", None)

    @_llm_call_observer.setter
    def _llm_call_observer(self, value: Any) -> None:
        # LLMProvider.__init__ assigns it; the inner provider keeps the real one.
        return None

    def _sampling_support(
        self, model: str | None, reasoning_effort: str | None, sampling: Sampling | None = None,
    ) -> frozenset[str] | None:
        return self._inner._sampling_support(model, reasoning_effort, sampling)

    def can_resume_conversation_state(self, state: Any, model: str | None = None) -> bool:
        return self._inner.can_resume_conversation_state(state, model)

    def supports_native_compaction(self, model: str | None = None) -> bool:
        return self._inner.supports_native_compaction(model)

    # -- metered entry points ---------------------------------------------------------

    async def _metered(
        self, call: Callable[[], Any], args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> LLMResponse:
        if current_call_attribution() is not None:
            return await call()  # the LLM layer admitted this logical call already
        estimate = self._estimator(_call_kwargs(args, kwargs))
        admission = self._metering.admit(estimate)
        attribution = CallAttribution(
            call_id=estimate.call_id, alias=self._alias, on_event=admission.settle,
        )
        try:
            with call_attribution(attribution):
                return await call()
        finally:
            admission.release()

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(lambda: self._inner.chat(*args, **kwargs), args, kwargs)

    async def chat_stream(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(lambda: self._inner.chat_stream(*args, **kwargs), args, kwargs)

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(
            lambda: self._inner.chat_with_retry(*args, **kwargs), args, kwargs,
        )

    async def chat_stream_with_retry(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(
            lambda: self._inner.chat_stream_with_retry(*args, **kwargs), args, kwargs,
        )

    async def chat_with_context(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(
            lambda: self._inner.chat_with_context(*args, **kwargs), args, kwargs,
        )

    async def chat_stream_with_context(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._metered(
            lambda: self._inner.chat_stream_with_context(*args, **kwargs), args, kwargs,
        )


__all__ = [
    "IMAGE_TOKENS",
    "REPROMPT_OVERHEAD_TOKENS",
    "Admission",
    "Budget",
    "BudgetedProvider",
    "CallEstimate",
    "CapBudget",
    "Metering",
    "ResponseCache",
    "build_estimate",
    "count_images",
    "price_tokens",
    "prompt_tokens_of",
]
