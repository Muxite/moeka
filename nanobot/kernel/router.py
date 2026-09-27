"""Cost-aware router (I6, design section 6): solver -> fast tier -> verify -> escalate.

The cascade for one sub-task, attributed to a schema *slot*:

1. Solvers first. With a ``task_type``, the deterministic-solver registry
   (:mod:`nanobot.kernel.solvers`) is tried; a match returns with zero model calls.
   A solved value is not passed to ``verify`` (deterministic output is the answer).
2. The start tier. The tier ladder is the config's model presets that declare a
   ``tier``, cheapest first (``local < fast < standard < frontier``), one preset per
   tier (the first in config order). The start tier is the slot's ``start`` (or an
   explicit ``tier=``), else ``"fast"`` clamped to the slot's ceiling; a start tier
   with no preset moves to the nearest configured tier inside the ceiling. A config
   with no tiered preset dispatches the active preset untiered (the ceiling cannot
   be checked; the ``model.route`` event says ``check="untiered"``).
3. ``verify(value)`` (sync or async). ``None`` accepts as-is; ``True`` returns;
   ``False`` (or a raising verifier) is a recorded verification failure and
   escalates exactly one configured tier up, at most ``max_escalations`` times
   (default 1). No higher tier: the result returns with ``verified=False``.

The ceiling and ``model.dispatch`` (design section 5 capability table):

- A slot's ceiling comes from ``Config.router.slots[slot].ceiling``. No ceiling (the
  default for every slot) means any tier, and ``model.dispatch`` is never checked.
- An escalation justified by a recorded verification failure may go above the
  ceiling without asking the policy ("audited, not asked"): the ``model.route``
  event says ``check="justified"``.
- A dispatch above the ceiling WITHOUT a recorded failure (an explicit ``tier=`` or
  a slot ``start`` above the ceiling) is a ``model.dispatch`` capability request
  (resource = the tier) through the same :func:`nanobot.kernel.gate.gate_call` the
  tool path uses: floors, then ``policy.decide``, a ``policy.decision`` event, and on
  ``Deny`` a deferred-log entry (design 5a). The ``GateResult`` has
  ``layer="policy"``, so a runner that ever routes classifies it as
  ``violation:policy:model.dispatch`` like any policy denial. The router itself runs
  outside a turn and charges no I5 budget.
- ``policy=None`` enforces the ceiling: the router then uses a ``DefaultPolicy``
  that denies ``model.dispatch``. A host that passes its own policy decides (the
  permissive ``DefaultPolicy()`` allows, audited).

Every routing decision emits one ``model.route`` event to the env's trace sink.
Model calls run under ``llm_usage_slot(slot)``, so their ``model.call`` ledger events
carry the slot.

Import discipline: stdlib, loguru, ``nanobot.llm_usage`` and the stdlib-only kernel
modules (``policy``, ``trace``) at module level; config, gate and the completion API
are imported lazily (Ruling C).
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.kernel.policy import CapabilityRequest, DefaultPolicy, PermissionPolicy, Principal
from nanobot.kernel.trace import LoguruTraceSink, safe_emit
from nanobot.llm_usage.context import llm_usage_slot

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.gate import GateResult

MODEL_DISPATCH = "model.dispatch"
ROUTE_EVENT = "model.route"
# Must equal ``typing.get_args(nanobot.config.schema.ModelTier)`` (pinned by a test).
TIER_ORDER: tuple[str, ...] = ("local", "fast", "standard", "frontier")
DEFAULT_START_TIER = "fast"

Verifier = Callable[[Any], "bool | Awaitable[bool]"]


def tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        raise ValueError(f"unknown model tier {tier!r}; expected one of {TIER_ORDER}") from None


@dataclass(frozen=True)
class TierLadder:
    """``(tier, preset_name)`` pairs, cheapest first, one preset per tier."""

    rungs: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_config(cls, config: Any) -> TierLadder:
        first: dict[str, str] = {}
        for name, preset in (getattr(config, "model_presets", None) or {}).items():
            tier = getattr(preset, "tier", None)
            if tier in TIER_ORDER and tier not in first:
                first[tier] = name
        return cls(tuple((t, first[t]) for t in TIER_ORDER if t in first))

    @property
    def tiers(self) -> tuple[str, ...]:
        return tuple(t for t, _ in self.rungs)

    def preset_for(self, tier: str) -> str | None:
        return dict(self.rungs).get(tier)

    def next_above(self, tier: str) -> str | None:
        rank = tier_rank(tier)
        return next((t for t in self.tiers if tier_rank(t) > rank), None)


@dataclass(frozen=True)
class DispatchTarget:
    """What one model call should run on: a tier's preset (``None`` = the active one)."""

    slot: str
    tier: str | None
    preset: str | None


@dataclass(frozen=True)
class RouteAttempt:
    tier: str | None
    preset: str | None
    check: str
    verified: bool | None


@dataclass(frozen=True)
class Escalation:
    """One step up the ladder. ``justified`` = backed by a recorded verification failure."""

    from_tier: str
    to_tier: str
    reason: str
    justified: bool = True


@dataclass(frozen=True)
class RouteResult:
    slot: str
    task_type: str | None
    value: Any = None
    tier: str | None = None
    preset: str | None = None
    solved_by: str | None = None
    verified: bool | None = None
    attempts: tuple[RouteAttempt, ...] = ()
    escalations: tuple[Escalation, ...] = ()
    denial: GateResult | None = None

    @property
    def calls(self) -> int:
        """Model dispatches made (0 for a solved or denied route)."""
        return len(self.attempts)

    @property
    def denied(self) -> bool:
        return self.denial is not None

    def error_text(self) -> str:
        return self.denial.error_text() if self.denial is not None else ""


class ModelDispatchDeniedError(PermissionError):
    """``think_structured``: the router's ``model.dispatch`` request was denied."""

    def __init__(self, result: RouteResult) -> None:
        super().__init__(result.error_text())
        self.result = result


class RouteVerificationError(ValueError):
    """``think_structured``: no dispatched tier produced a value ``verify`` accepted."""

    def __init__(self, result: RouteResult) -> None:
        super().__init__(
            f"slot {result.slot!r}: verification failed at every tried tier "
            f"({', '.join(str(a.tier) for a in result.attempts)})"
        )
        self.result = result


class _ModelDispatchCall:
    """The pseudo-tool the router hands to ``gate_call``: one ``model.dispatch`` request."""

    name = MODEL_DISPATCH

    def __init__(self, tier: str) -> None:
        self._tier = tier

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest(MODEL_DISPATCH, self._tier)]


def ceiling_policy() -> PermissionPolicy:
    """The policy used when the caller passes none: a configured ceiling is enforced."""
    return DefaultPolicy(deny_capabilities=frozenset({MODEL_DISPATCH}))


def _slot_route(config: Any, slot: str) -> tuple[str | None, str | None]:
    router = getattr(config, "router", None)
    slots = getattr(router, "slots", None) or {}
    entry = slots.get(slot)
    if entry is None:
        return None, None
    return getattr(entry, "ceiling", None), getattr(entry, "start", None)


def _within(tier: str, ceiling: str | None) -> bool:
    return ceiling is None or tier_rank(tier) <= tier_rank(ceiling)


def _start_tier(
    ladder: TierLadder, requested: str | None, explicit: bool, ceiling: str | None,
) -> str | None:
    if not ladder.rungs:
        return None
    if explicit:
        if requested not in ladder.tiers:
            raise ValueError(
                f"no model preset is configured for tier {requested!r} "
                f"(configured: {', '.join(ladder.tiers)})"
            )
        return requested
    target = requested or DEFAULT_START_TIER
    if ceiling is not None and tier_rank(target) > tier_rank(ceiling):
        target = ceiling
    inside = [t for t in ladder.tiers if _within(t, ceiling)]
    if not inside:
        return ladder.tiers[0]  # everything is above the ceiling: checked below
    return next((t for t in inside if tier_rank(t) >= tier_rank(target)), inside[-1])


async def _verify(verify: Verifier, value: Any) -> tuple[bool, str]:
    try:
        verdict = verify(value)
        if inspect.isawaitable(verdict):
            verdict = await verdict
    except Exception as exc:  # noqa: BLE001 - a broken verifier is a failed verification
        logger.warning("route verifier raised {}: {}; treated as failed", type(exc).__name__, exc)
        return False, "verification_error"
    return bool(verdict), "verification_failed"


def _default_dispatch(
    *,
    prompt: str,
    config: Any,
    env: CoreEnvironment | None,
    model_cls: type | None,
    complete_kwargs: dict[str, Any],
) -> Callable[[DispatchTarget], Awaitable[Any]]:
    async def _dispatch(target: DispatchTarget) -> Any:
        complete_api = importlib.import_module("nanobot.api.complete")

        kwargs = dict(complete_kwargs)
        if target.preset is not None:
            kwargs["preset"] = target.preset
        if env is not None:
            kwargs["env"] = env
        # The solvers already ran in the router: no task_type is forwarded.
        return await complete_api.acomplete_json(
            prompt, model_cls=model_cls, config=config, **kwargs,
        )

    return _dispatch


def _solve(
    task_type: str, payload: dict[str, Any] | None, prompt: str, model_cls: type | None,
) -> tuple[Any, str | None]:
    """The ``acomplete_json`` solver path (same lookup, same coercion), run once."""
    complete_api = importlib.import_module("nanobot.api.complete")

    value, solver_name = complete_api._solve_deterministic(task_type, payload, prompt, model_cls)
    return (value, solver_name) if solver_name is not None else (None, None)


async def route(
    slot: str,
    task_type: str | None,
    payload: dict[str, Any] | None,
    *,
    prompt: str | None = None,
    verify: Verifier | None = None,
    tier: str | None = None,
    max_escalations: int = 1,
    config: Any | None = None,
    model_cls: type | None = None,
    dispatch: Callable[[DispatchTarget], Awaitable[Any]] | None = None,
    policy: PermissionPolicy | None = None,
    principal: Principal | None = None,
    env: CoreEnvironment | None = None,
    **complete_kwargs: Any,
) -> RouteResult:
    """Route one sub-task through the cascade (module docstring) and return the outcome.

    *payload* is the solver payload (``{"prompt": prompt}`` when ``None``); *prompt*
    defaults to ``payload["prompt"]``. *dispatch* replaces the default model call
    (:func:`nanobot.api.complete.acomplete_json` on the tier's preset, with *env*
    so the ledger measures it); the rest of *complete_kwargs* (``schema``,
    ``system``, ``retries``, ...) is forwarded to it. A dispatch exception
    propagates (it is not a verification failure). A ``model.dispatch`` denial is
    returned as ``RouteResult.denial``, never raised.
    """
    if not isinstance(slot, str) or not slot:
        raise ValueError("slot must be a non-empty string")
    if prompt is None:
        prompt = str((payload or {}).get("prompt", ""))
    sink = env.trace if env is not None else LoguruTraceSink()
    base_event = {"event": ROUTE_EVENT, "slot": slot, "task_type": task_type}

    if task_type is not None:
        value, solver_name = _solve(task_type, payload, prompt, model_cls)
        if solver_name is not None:
            safe_emit(sink, {**base_event, "tier": None, "preset": None, "ceiling": None,
                             "reason": "solved", "check": "solver", "verdict": "allow",
                             "solver": solver_name})
            return RouteResult(slot=slot, task_type=task_type, value=value,
                               solved_by=solver_name)

    # After the solvers: a solved task never loads config, as in acomplete_json.
    if config is None:
        from nanobot.config.loader import config_from_sources

        config, _ = config_from_sources(
            config=None,
            config_dict=complete_kwargs.pop("config_dict", None),
            config_path=complete_kwargs.pop("config_path", None),
        )
    else:
        complete_kwargs.pop("config_dict", None)
        complete_kwargs.pop("config_path", None)
    ladder = TierLadder.from_config(config)
    ceiling, slot_start = _slot_route(config, slot)
    explicit = tier is not None or slot_start is not None
    current = _start_tier(ladder, tier or slot_start, explicit, ceiling)
    run = dispatch or _default_dispatch(
        prompt=prompt, config=config, env=env, model_cls=model_cls,
        complete_kwargs=complete_kwargs,
    )

    attempts: list[RouteAttempt] = []
    escalations: list[Escalation] = []
    reason = "initial"
    from_tier: str | None = None
    while True:
        preset = ladder.preset_for(current) if current is not None else None
        if current is None:
            check = "untiered"
        elif ceiling is None:
            check = "no_ceiling"
        elif _within(current, ceiling):
            check = "within_ceiling"
        elif reason != "initial":
            check = "justified"  # a recorded verification failure: audited, not asked
        else:
            check = "policy"
        event = {**base_event, "tier": current, "preset": preset, "ceiling": ceiling,
                 "reason": reason, "check": check, "from_tier": from_tier}
        if check == "policy":
            from nanobot.kernel.gate import gate_call

            gated = gate_call(
                _ModelDispatchCall(current),
                {"slot": slot, "tier": current, "preset": preset, "task_type": task_type,
                 "ceiling": ceiling},
                principal,
                policy if policy is not None else ceiling_policy(),
                env,
            )
            if not gated.allowed:
                safe_emit(sink, {**event, "verdict": "deny"})
                return RouteResult(
                    slot=slot, task_type=task_type, tier=current, preset=preset,
                    attempts=tuple(attempts), escalations=tuple(escalations), denial=gated,
                )
        safe_emit(sink, {**event, "verdict": "allow"})

        with llm_usage_slot(slot):
            value = await run(DispatchTarget(slot=slot, tier=current, preset=preset))

        if verify is None:
            attempts.append(RouteAttempt(current, preset, check, None))
            return RouteResult(slot=slot, task_type=task_type, value=value, tier=current,
                               preset=preset, verified=None, attempts=tuple(attempts),
                               escalations=tuple(escalations))
        ok, failure = await _verify(verify, value)
        attempts.append(RouteAttempt(current, preset, check, ok))
        nxt = ladder.next_above(current) if current is not None else None
        if ok or nxt is None or len(escalations) >= max(0, max_escalations):
            if not ok:
                safe_emit(sink, {**base_event, "tier": current, "preset": preset,
                                 "ceiling": ceiling, "reason": failure,
                                 "check": "no_escalation", "verdict": "none",
                                 "from_tier": current})
            return RouteResult(slot=slot, task_type=task_type, value=value, tier=current,
                               preset=preset, verified=ok, attempts=tuple(attempts),
                               escalations=tuple(escalations))
        escalations.append(Escalation(from_tier=current, to_tier=nxt, reason=failure))
        from_tier, current, reason = current, nxt, failure


__all__ = [
    "DEFAULT_START_TIER",
    "MODEL_DISPATCH",
    "ROUTE_EVENT",
    "TIER_ORDER",
    "DispatchTarget",
    "Escalation",
    "ModelDispatchDeniedError",
    "RouteAttempt",
    "RouteResult",
    "RouteVerificationError",
    "TierLadder",
    "Verifier",
    "ceiling_policy",
    "route",
    "tier_rank",
]
