"""Deterministic-solver registry (I6, design section 6).

A task that can be solved without a model must never invoke one. Plugins and
hosts register solvers per task type; the kernel owns only the lookup.

Contract:
- A solver is ``fn(payload: dict) -> Solved | None``. ``None`` means "cannot solve
  this particular payload"; the caller falls through to the next solver, then to
  the LLM.
- A task type may carry several solvers. :func:`try_solve` tries them in
  registration order; the first ``Solved`` wins.
- An ``input_schema`` given at registration is checked (JSON Schema subset, the
  same validator tools use) before that solver runs; a rejected payload skips
  only that solver.
- A solver that raises, or returns anything other than ``Solved``, is logged and
  treated as "no solution". A broken solver never crashes the caller.

Registration is process-wide (solvers are registered once at startup, not
per-request state), via :func:`register_solver` on the default registry. An
explicit :class:`SolverRegistry` exists for hosts and tests that want isolation.

Import discipline: stdlib + loguru at module level. The JSON-schema validator
lives in ``nanobot.agent.tools.base`` and is imported lazily (Ruling C).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class Solved:
    """A deterministic answer: ``value`` produced by the solver named ``solver_name``."""

    value: Any
    solver_name: str


SolverFn = Callable[[dict[str, Any]], "Solved | None"]


@dataclass(frozen=True)
class _Entry:
    fn: SolverFn
    input_schema: dict[str, Any] | None
    name: str


def _solver_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


def _schema_errors(payload: Any, schema: dict[str, Any]) -> list[str]:
    from nanobot.agent.tools.base import Schema

    return Schema.validate_json_schema_value(payload, schema)


class SolverRegistry:
    """Task type -> ordered solvers. Thread-safe registration; lookups snapshot the list."""

    def __init__(self) -> None:
        self._solvers: dict[str, list[_Entry]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        task_type: str,
        fn: SolverFn,
        *,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(task_type, str) or not task_type:
            raise ValueError("task_type must be a non-empty string")
        if not callable(fn):
            raise TypeError("solver must be callable")
        entry = _Entry(fn=fn, input_schema=input_schema, name=_solver_name(fn))
        with self._lock:
            self._solvers.setdefault(task_type, []).append(entry)

    def has(self, task_type: str) -> bool:
        return bool(self._solvers.get(task_type))

    def clear(self) -> None:
        with self._lock:
            self._solvers.clear()

    def try_solve(self, task_type: str, payload: dict[str, Any]) -> Solved | None:
        """First ``Solved`` from the task type's solvers, in registration order, else None."""
        entries = list(self._solvers.get(task_type, ()))
        for entry in entries:
            if entry.input_schema is not None:
                try:
                    errors = _schema_errors(payload, entry.input_schema)
                except Exception:
                    logger.opt(exception=True).warning(
                        "solver {} ({}): input_schema check failed; skipped",
                        entry.name, task_type,
                    )
                    continue
                if errors:
                    logger.debug(
                        "solver {} ({}): payload rejected by input_schema: {}",
                        entry.name, task_type, "; ".join(errors),
                    )
                    continue
            try:
                result = entry.fn(payload)
            except Exception as exc:
                logger.warning(
                    "solver {} ({}) raised {}: {}; treated as no solution",
                    entry.name, task_type, type(exc).__name__, exc,
                )
                continue
            if result is None:
                continue
            if not isinstance(result, Solved):
                logger.warning(
                    "solver {} ({}) returned {}, not Solved; treated as no solution",
                    entry.name, task_type, type(result).__name__,
                )
                continue
            return result
        return None


_DEFAULT_REGISTRY = SolverRegistry()


def default_registry() -> SolverRegistry:
    """The process-wide registry used by :func:`register_solver` and :func:`try_solve`."""
    return _DEFAULT_REGISTRY


def register_solver(
    task_type: str,
    fn: SolverFn,
    *,
    input_schema: dict[str, Any] | None = None,
) -> None:
    """Register ``fn`` for ``task_type`` on the default registry (appended after earlier ones)."""
    _DEFAULT_REGISTRY.register(task_type, fn, input_schema=input_schema)


def try_solve(task_type: str, payload: dict[str, Any]) -> Solved | None:
    """Look up ``task_type`` on the default registry; see :meth:`SolverRegistry.try_solve`."""
    return _DEFAULT_REGISTRY.try_solve(task_type, payload)


__all__ = [
    "Solved",
    "SolverFn",
    "SolverRegistry",
    "default_registry",
    "register_solver",
    "try_solve",
]
