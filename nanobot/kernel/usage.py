"""``kernel.usage``: the consumer-facing usage surface (spec 001).

One object answers "what did my calls use and cost" without a ledger of the consumer's
own: query stored usage by consumer, agent, session, role, purpose, model, trace and
time; totals with group-by; the budget in force; and a live subscription.

Everything here is content-free and fail-open: a failing subscriber or a failing store
write is counted (:meth:`UsageView.loss`) and never reaches a model call.

Sources: ``model.call`` events are projected into ``llm_calls`` by the ledger; ``cache.hit``,
``budget.refuse`` and ``call.waste`` events are projected into ``llm_usage_events`` by the
kernel's own subscriber (:class:`StoreProjection`), so the stream and the store carry the
same facts.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.kernel.ledger import (
    DOCUMENT_VERSIONS,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    UNATTRIBUTED,
)
from nanobot.kernel.trace import safe_emit
from nanobot.llm_usage.query import UsageFilter, UsageTotals, records, totals

if TYPE_CHECKING:
    from nanobot.kernel.kernel import Kernel

#: Trace events the usage surface speaks.
USAGE_EVENTS = ("model.call", "cache.hit", "budget.admit", "budget.refuse", "call.waste")
WASTE_LABELS = (
    "retry", "failover", "avoidable_cache_miss", "discarded_draft", "refused_after_admission",
)
_STORED_EVENTS = ("cache.hit", "budget.refuse", "call.waste")


class StoreProjection:
    """Writes the non-call usage events to the usage store; counts failures (fail-open)."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self.failures = 0
        self._unsubscribe = [
            kernel.trace.subscribe(name, self._on_event) for name in _STORED_EVENTS
        ]

    def _on_event(self, event: dict[str, Any]) -> None:
        try:
            from nanobot.llm_usage import get_llm_usage_store

            if "started_at_ms" not in event and isinstance(event.get("ts"), (int, float)):
                event["started_at_ms"] = int(event["ts"] * 1000)
            store = get_llm_usage_store(data_dir=self._kernel.core_env.paths.data_dir)
            store.record_usage_event(event)
        except Exception:  # noqa: BLE001 - observation never fails a call
            self.failures += 1
            logger.exception("usage store write failed for {}", event.get("event"))

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []


class Subscription:
    """A live feed of usage events delivered on its own thread.

    Events go through a bounded queue: when it is full the event is dropped and counted
    (:attr:`dropped`), so a slow or blocked callback never delays a model call. A callback
    that raises is counted in :attr:`errors` and the feed continues.
    """

    def __init__(
        self, view: UsageView, fn: Callable[[dict[str, Any]], None],
        kinds: tuple[str, ...], max_queue: int,
    ) -> None:
        self._fn = fn
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.errors = 0
        self.delivered = 0
        self._closed = False
        self._unsubscribe = [view._kernel.trace.subscribe(k, self._offer) for k in kinds]  # noqa: SLF001
        self._thread = threading.Thread(target=self._run, name="moeka-usage-sub", daemon=True)
        self._thread.start()

    def _offer(self, event: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                try:
                    self._fn(event)
                    self.delivered += 1
                except Exception:  # noqa: BLE001
                    self.errors += 1
                    logger.exception("usage subscriber failed")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything offered so far has been delivered (or *timeout* passed)."""
        done = threading.Event()

        def wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=wait, daemon=True).start()
        return done.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass  # daemon thread; the process end reaps it

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class UsageView:
    """``kernel.usage``. Reads the kernel's own usage store; see the module docstring."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._projection = StoreProjection(kernel)
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    # -- schema ---------------------------------------------------------------------

    @property
    def schema_version(self) -> str:
        """The ``usage-record`` schema version this kernel emits."""
        return SCHEMA_VERSION

    def schema_versions(self) -> tuple[str, ...]:
        """Every ``usage-record`` version this kernel emits."""
        return SUPPORTED_SCHEMA_VERSIONS

    def documents(self) -> dict[str, str]:
        """The version each document type is emitted at (``usage-record``, ``budget-event``,
        ``waste-label``, ``complete-json-call``): they version independently."""
        return dict(DOCUMENT_VERSIONS)

    # -- queries --------------------------------------------------------------------

    def _store(self) -> Any:
        from nanobot.llm_usage import get_llm_usage_store

        return get_llm_usage_store(data_dir=self._kernel.core_env.paths.data_dir)

    def totals(
        self, group_by: Iterable[str] = (), /, **filters: Any,
    ) -> list[UsageTotals]:
        """Usage totals for the calls matching *filters*, optionally grouped.

        *filters*: ``consumer, agent, session, role, purpose, model, provider, trace_id,
        call_id, since_ms, until_ms``. *group_by*: any of ``consumer, agent, session, role,
        purpose, model, provider, trace_id, tier, outcome, waste_label, alias, source``.
        """
        return totals(self._store(), UsageFilter(**filters), tuple(group_by))

    def total(self, **filters: Any) -> UsageTotals:
        """The single ungrouped :class:`UsageTotals` for *filters*."""
        return self.totals((), **filters)[0]

    def records(self, *, limit: int = 100, **filters: Any) -> list[dict[str, Any]]:
        """Newest ``usage-record.v1`` documents matching *filters* (calls and cache hits)."""
        return records(self._store(), UsageFilter(**filters), limit=limit)

    def budget(self) -> dict[str, Any] | None:
        """The budget in force (``budget-event.v1`` figures), or ``None`` without one.

        A budget without a ``snapshot()`` method reports ``None`` figures it cannot give.
        """
        budget = self._kernel.budget
        if budget is None:
            return None
        snapshot = getattr(budget, "snapshot", None)
        if not callable(snapshot):
            return None
        return {"schema_version": DOCUMENT_VERSIONS["budget-event"], **snapshot()}

    # -- waste ----------------------------------------------------------------------

    def label_waste(
        self, call_id: str, label: str, *, attempt: int | None = None, set_by: str = "caller",
    ) -> None:
        """Mark a call's tokens as wasted once the caller knows (a discarded draft).

        Appends a ``call.waste`` event; past events are never mutated and queries fold the
        label in. *label* is one of :data:`WASTE_LABELS`.
        """
        if label not in WASTE_LABELS:
            raise ValueError(f"unknown waste label {label!r}; choose from {WASTE_LABELS}")
        safe_emit(self._kernel.trace, {
            "event": "call.waste", "schema_version": DOCUMENT_VERSIONS["waste-label"],
            "call_id": call_id,
            "attempt": attempt, "waste_label": label, "waste_set_by": set_by,
        })

    # -- live -----------------------------------------------------------------------

    def subscribe(
        self,
        fn: Callable[[dict[str, Any]], None],
        *,
        kinds: Iterable[str] = USAGE_EVENTS,
        max_queue: int = 1000,
    ) -> Subscription:
        """Deliver usage events (``model.call``, ``cache.hit``, ``budget.*``, ``call.waste``)
        to *fn* on a separate thread. Never blocks or fails a model call; see :class:`Subscription`."""
        sub = Subscription(self, fn, tuple(kinds), max(1, int(max_queue)))
        with self._lock:
            self._subs.append(sub)
        return sub

    def loss(self) -> dict[str, int]:
        """What observation lost or could not keep, as counters (all zero when whole).

        ``store_failures``: writes that failed (a locked or full database); ``pending_writes``:
        of those, still waiting to be retried with the next write; ``write_dropped``: lost for
        good (retry buffer full); ``dropped_events`` / ``subscriber_errors``: live subscribers;
        ``duplicates``: duplicate deliveries ignored; ``pruned_rows``: rows retention removed
        (a total over a window older than ``store.stats()["oldest_started_at_ms"]`` is
        incomplete); ``unattributed_requests``: calls no layer attributed (a defect).
        """
        with self._lock:
            subs = list(self._subs)
        stats = self._store().stats()
        return {
            "store_failures": self._projection.failures + stats["write_failures"],
            "pending_writes": stats["pending_writes"],
            "write_dropped": stats["write_dropped"],
            "dropped_events": sum(s.dropped for s in subs),
            "subscriber_errors": sum(s.errors for s in subs),
            "duplicates": stats["duplicates"],
            "pruned_rows": stats["pruned_rows"],
            "unattributed_requests": self.total(consumer=UNATTRIBUTED).requests,
        }

    # -- tamper evidence ------------------------------------------------------------

    def checkpoint(self) -> dict[str, Any]:
        """A digest of the stored rows to keep OUTSIDE the agent's reach (the harness's own
        state). An exec-capable agent can rewrite the usage database; it cannot make an edited
        database match a digest it never saw. See :meth:`verify`."""
        return self._store().checkpoint()

    def verify(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """``{"ok": bool, "reason": None | "rows_missing" | "rows_changed"}`` against a
        :meth:`checkpoint`. Honest appends since the checkpoint are fine."""
        return self._store().verify(checkpoint)

    def _close(self) -> None:
        with self._lock:
            subs, self._subs = self._subs, []
        for sub in subs:
            sub.close()
        self._projection.close()


__all__ = ["USAGE_EVENTS", "WASTE_LABELS", "Subscription", "UsageFilter", "UsageTotals", "UsageView"]
