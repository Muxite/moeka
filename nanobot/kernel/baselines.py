"""Baseline comparators for I6 (design section 6, "How I6 is measured").

Each eval task family declares its minimal adequate alternative: the cheapest
approach that meets the family's quality target (one zero-shot call, a regex/AST
parser, a static heuristic). The RSI harness scores a moeka run against it with
:func:`cost_ratio`: actual cost / baseline cost, from the run's ledger events.

Ratio rules (strict by default, because a ratio is a claim about money spent):
- ``None`` when the baseline's reference cost is unknown.
- ``None`` when any event's ``cost_usd`` is unknown (a missing price is unknown,
  never free; see :mod:`nanobot.kernel.ledger`).
- ``None`` when any event's cost is not billed ground truth (Ruling J: provider
  reported no usage and the tokens were estimated locally, or an old trace record
  carries no ``usage_source``). :func:`compare_cost` with ``allow_estimated=True``
  computes the ratio anyway and sets ``estimated=True`` so the harness can flag it.
- A local-tier ``0.0`` counts as billed whatever the usage source (the zero-cost
  convention does not depend on tokens).
- No events (a solver answered) means an actual cost of ``0.0``.
- A deterministic baseline costs ``0.0``: a run that also spent nothing scores
  ``1.0`` (parity); any spend scores ``math.inf`` (I6: a deterministic task must
  never invoke a model).

Module-level imports: stdlib only. Not wired into any live path; the harness uses it.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BaselineKind = str  # "deterministic" | "zero_shot" | "heuristic" | "other"


@dataclass(frozen=True)
class Baseline:
    """A task family's declared minimal adequate alternative."""

    family: str
    alternative: str
    kind: BaselineKind = "zero_shot"
    reference_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.family, str) or not self.family:
            raise ValueError("baseline family must be a non-empty string")
        if self.reference_cost_usd is not None and self.reference_cost_usd < 0:
            raise ValueError("baseline reference_cost_usd must be non-negative")

    @classmethod
    def deterministic(cls, family: str, alternative: str) -> Baseline:
        """A parser/validator baseline: zero model cost."""
        return cls(family, alternative, kind="deterministic", reference_cost_usd=0.0)


@dataclass(frozen=True)
class CostComparison:
    """The full answer behind :func:`cost_ratio`, for the harness to report."""

    ratio: float | None
    actual_usd: float | None
    baseline_usd: float | None
    events: int
    unbilled: int
    estimated: bool = False
    reason: str | None = None  # why ``ratio`` is None


def _fields(event: Any) -> tuple[float | None, str, str | None]:
    """``(cost_usd, usage_source, tier)`` from a ``LedgerEvent`` or its trace dict."""
    if isinstance(event, Mapping):
        return event.get("cost_usd"), event.get("usage_source") or "none", event.get("tier")
    return (
        getattr(event, "cost_usd", None),
        getattr(event, "usage_source", None) or "none",
        getattr(event, "tier", None),
    )


def _billed(cost: float | None, usage_source: str, tier: str | None) -> bool:
    # Mirrors ``LedgerEvent.cost_is_billed`` so trace dicts are judged the same way.
    if cost is None:
        return False
    if tier == "local" and cost == 0.0:
        return True
    return usage_source == "reported"


def compare_cost(
    run_events: Iterable[Any],
    baseline: Baseline,
    *,
    allow_estimated: bool = False,
) -> CostComparison:
    """Compare a run's ledger events with *baseline* (module docstring rules)."""
    rows = [_fields(e) for e in run_events]
    unbilled = sum(1 for cost, source, tier in rows if not _billed(cost, source, tier))
    base = baseline.reference_cost_usd
    if any(cost is None for cost, _, _ in rows):
        return CostComparison(None, None, base, len(rows), unbilled, reason="unknown_cost")
    actual = float(sum(cost for cost, _, _ in rows if cost is not None))
    if unbilled and not allow_estimated:
        return CostComparison(None, actual, base, len(rows), unbilled, estimated=True,
                              reason="estimated_usage")
    if base is None:
        return CostComparison(None, actual, None, len(rows), unbilled,
                              estimated=bool(unbilled), reason="unknown_baseline_cost")
    if base == 0.0:
        ratio = 1.0 if actual == 0.0 else math.inf
    else:
        ratio = actual / base
    return CostComparison(ratio, actual, base, len(rows), unbilled, estimated=bool(unbilled))


def cost_ratio(
    run_events: Iterable[Any],
    baseline: Baseline,
    *,
    allow_estimated: bool = False,
) -> float | None:
    """Actual cost / baseline cost, or ``None`` when it cannot be known honestly."""
    return compare_cost(run_events, baseline, allow_estimated=allow_estimated).ratio


class BaselineRegistry:
    """Task family -> its declared :class:`Baseline` (one per family)."""

    def __init__(self) -> None:
        self._baselines: dict[str, Baseline] = {}
        self._lock = threading.Lock()

    def register(self, baseline: Baseline, *, replace: bool = False) -> None:
        with self._lock:
            if baseline.family in self._baselines and not replace:
                raise ValueError(f"baseline for family {baseline.family!r} already declared")
            self._baselines[baseline.family] = baseline

    def get(self, family: str) -> Baseline | None:
        return self._baselines.get(family)

    def families(self) -> tuple[str, ...]:
        return tuple(self._baselines)


_DEFAULT_REGISTRY = BaselineRegistry()


def default_registry() -> BaselineRegistry:
    """The process-wide registry used by :func:`declare_baseline` and :func:`get_baseline`."""
    return _DEFAULT_REGISTRY


def declare_baseline(baseline: Baseline, *, replace: bool = False) -> None:
    """Declare *baseline* on the process-wide registry."""
    _DEFAULT_REGISTRY.register(baseline, replace=replace)


def get_baseline(family: str) -> Baseline | None:
    return _DEFAULT_REGISTRY.get(family)


__all__ = [
    "Baseline",
    "BaselineRegistry",
    "CostComparison",
    "compare_cost",
    "cost_ratio",
    "declare_baseline",
    "default_registry",
    "get_baseline",
]
