"""Spend and cache hooks: ``Kernel(env, budget=..., cache=...)``.

``CapBudget`` is in-memory and per process; ``SharedCapBudget`` keeps its caps, spend and
reservations in the shared usage store, so every process and container on one data dir
spends against one cap (spec 005).

``ModelCallEvent`` is what ``Budget.settle(reservation, event)`` receives: one physical
attempt's ``model.call`` ledger event (tokens, ``cost_usd``, tags, ...).
"""

from nanobot.kernel.budget import Budget, CallEstimate, CapBudget, ResponseCache
from nanobot.kernel.budget_shared import SharedCapBudget
from nanobot.kernel.ledger import LedgerEvent as ModelCallEvent

__all__ = [
    "Budget", "CallEstimate", "CapBudget", "ModelCallEvent", "ResponseCache", "SharedCapBudget",
]
