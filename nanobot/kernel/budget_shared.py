"""``SharedCapBudget``: one spend cap across processes and containers (spec 005, H).

:class:`~nanobot.kernel.budget.CapBudget` keeps its figures in memory, so N workers
each get the whole cap. ``SharedCapBudget`` keeps caps, spend and open reservations in
the usage store ``<data_dir>/llm_usage.sqlite3`` (tables ``budget_caps``,
``budget_reservations``, ``budget_spend``), shared by every ``SharedCapBudget`` with the
same resolved ``data_dir`` and ``budget_id``, in any process or container on the host.

- Admission is one ``BEGIN IMMEDIATE`` transaction: expire lapsed leases, sum spend
  plus open reservations over all holders, apply ``CapBudget``'s checks (the shared
  :func:`~nanobot.kernel.budget.check_admission`), insert the reservation. So at every
  committed state ``spent + reserved <= cap`` (as long as no settled attempt costs
  more than its reservation).
- ``settle`` / ``release`` follow ``CapBudget`` exactly (Q1: an unpriced, timed-out or
  cancelled attempt charges its remainder at release).
- A reservation whose lease passed (its holder died, or it ran past ``lease_s``) is
  charged: its remainder counts as spent, a ``budget.expire`` event is emitted by the
  process that marks it, and a late settle/release trues it up so the final spend is
  what it would have been without the expiry.
- Fail closed: no write lock within ``lock_timeout_s`` refuses the admission with
  ``BudgetExceeded(reason_code="budget_unavailable")``. A settle/release that cannot be
  written is retried on the next operation and on :meth:`flush` (``Kernel.close()``);
  until then its reservation stays open, so caps never loosen.

Unsupported: a data dir on a network filesystem (SQLite locking), and hosts whose
clocks disagree (one host, one kernel clock).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.kernel.budget import (
    CallEstimate,
    _tag_slot,
    budget_figures,
    check_admission,
    refusal,
    settle_charge,
)
from nanobot.kernel.llm_errors import BudgetExceeded
from nanobot.kernel.trace import TraceSink, safe_emit

if TYPE_CHECKING:
    from nanobot.kernel.ledger import LedgerEvent

USAGE_DB_FILENAME = "llm_usage.sqlite3"

BUDGET_DDL = """
CREATE TABLE IF NOT EXISTS budget_caps (
    budget_id TEXT PRIMARY KEY,
    limit_usd REAL,
    limit_tokens INTEGER,
    per_tag TEXT NOT NULL DEFAULT '{}',
    allow_unpriced INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    id INTEGER PRIMARY KEY,
    budget_id TEXT NOT NULL,
    call_id TEXT,
    usd REAL NOT NULL,
    tokens INTEGER NOT NULL,
    slots TEXT NOT NULL DEFAULT '[]',
    holder TEXT,
    created_ms INTEGER NOT NULL,
    lease_expires_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    unpriced INTEGER NOT NULL DEFAULT 0,
    expired_charge_usd REAL NOT NULL DEFAULT 0,
    expired_charge_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS budget_reservations_open_idx
    ON budget_reservations(budget_id, state, lease_expires_ms);
CREATE TABLE IF NOT EXISTS budget_spend (
    budget_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    spent_usd REAL NOT NULL DEFAULT 0,
    spent_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (budget_id, scope_key, scope_value)
);
"""

_KERNEL_SCOPE = ("", "")

Slot = tuple[str, Hashable]


def _slot_text(value: Hashable) -> str:
    """A stable text form of a tag value (the row key of its spend scope)."""
    if isinstance(value, str):
        return "s:" + value
    if value is None or isinstance(value, (bool, int, float)):
        return "j:" + json.dumps(value)
    return "r:" + repr(value)


def _slots_json(slots: tuple[Slot, ...]) -> str:
    return json.dumps([[key, _slot_text(value)] for key, value in slots])


def _slots_from_json(text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    return [(str(item[0]), str(item[1])) for item in data if isinstance(item, list)]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_locked(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@dataclass(eq=False)
class SharedReservation:
    """The opaque reservation ``SharedCapBudget.admit`` returns (one row)."""

    row_id: int
    budget_id: str
    call_id: str
    usd: float
    tokens: int
    slots: tuple[Slot, ...]
    released: bool = False


@dataclass(frozen=True)
class _Op:
    kind: str  # "settle" | "release"
    reservation: SharedReservation
    tokens: int = 0
    cost: float | None = None
    unpriced: bool = False


class SharedCapBudget:
    """A cap :class:`~nanobot.kernel.budget.Budget` whose state lives in the usage store.

    Same caps and semantics as :class:`~nanobot.kernel.budget.CapBudget` (``limit_usd``,
    ``limit_tokens``, ``per_tag``, ``allow_unpriced``), shared by every instance with the
    same resolved *data_dir* and *budget_id*. The first construction for a
    ``budget_id`` stores its caps; a later one with different caps raises ``ValueError``
    unless ``reset_caps=True`` (replaces the caps, keeps spend and reservations).
    """

    def __init__(
        self,
        data_dir: str | Path,
        budget_id: str = "default",
        *,
        limit_usd: float | None = None,
        limit_tokens: int | None = None,
        per_tag: Mapping[str, float] | None = None,
        allow_unpriced: bool = False,
        lease_s: float = 3600.0,
        lock_timeout_s: float = 10.0,
        reset_caps: bool = False,
    ) -> None:
        if not isinstance(budget_id, str) or not budget_id:
            raise ValueError(f"budget_id must be a non-empty string, got {budget_id!r}")
        if lease_s <= 0:
            raise ValueError(f"lease_s must be positive, got {lease_s!r}")
        if lock_timeout_s < 0:
            raise ValueError(f"lock_timeout_s must be >= 0, got {lock_timeout_s!r}")
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / USAGE_DB_FILENAME
        self.budget_id = budget_id
        self.limit_usd = None if limit_usd is None else float(limit_usd)
        self.limit_tokens = None if limit_tokens is None else int(limit_tokens)
        self.per_tag: Mapping[str, float] = {k: float(v) for k, v in (per_tag or {}).items()}
        self.allow_unpriced = bool(allow_unpriced)
        self.lease_s = float(lease_s)
        self.lock_timeout_s = float(lock_timeout_s)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._conn_pid: int | None = None
        self._pending: list[_Op] = []
        self._sinks: list[TraceSink] = []
        self._holder = f"{socket.gethostname()}:{os.getpid()}:{_now_ms()}"
        self._store_caps(reset_caps)

    # -- plumbing ---------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        pid = os.getpid()
        if self._conn is not None and self._conn_pid == pid:
            return self._conn
        self._conn = None  # a forked child never reuses its parent's connection
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=self.lock_timeout_s, isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.lock_timeout_s * 1000)}")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # another connection holds it; WAL is persistent once set
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(BUDGET_DDL)
        self._conn = conn
        self._conn_pid = pid
        return conn

    def _begin(self) -> sqlite3.Connection:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def _store_caps(self, reset: bool) -> None:
        per_tag = json.dumps(dict(sorted(self.per_tag.items())))
        with self._lock:
            conn = self._begin()
            try:
                row = conn.execute(
                    "SELECT limit_usd, limit_tokens, per_tag, allow_unpriced FROM budget_caps "
                    "WHERE budget_id = ?", (self.budget_id,),
                ).fetchone()
                if row is None or reset:
                    conn.execute(
                        "INSERT INTO budget_caps (budget_id, limit_usd, limit_tokens, per_tag, "
                        "allow_unpriced, updated_ms) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(budget_id) DO UPDATE SET limit_usd = excluded.limit_usd, "
                        "limit_tokens = excluded.limit_tokens, per_tag = excluded.per_tag, "
                        "allow_unpriced = excluded.allow_unpriced, "
                        "updated_ms = excluded.updated_ms",
                        (self.budget_id, self.limit_usd, self.limit_tokens, per_tag,
                         int(self.allow_unpriced), _now_ms()),
                    )
                else:
                    stored = (
                        row["limit_usd"], row["limit_tokens"],
                        json.dumps(dict(sorted(json.loads(row["per_tag"] or "{}").items()))),
                        bool(row["allow_unpriced"]),
                    )
                    mine = (self.limit_usd, self.limit_tokens, per_tag, self.allow_unpriced)
                    if stored != mine:
                        raise ValueError(
                            f"budget_id {self.budget_id!r} already has different caps in "
                            f"{self.path} (stored limit_usd={stored[0]}, "
                            f"limit_tokens={stored[1]}, per_tag={stored[2]}, "
                            f"allow_unpriced={stored[3]}); pass reset_caps=True to replace them"
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _caps(self, conn: sqlite3.Connection) -> tuple[float | None, int | None, dict[str, float], bool]:
        row = conn.execute(
            "SELECT limit_usd, limit_tokens, per_tag, allow_unpriced FROM budget_caps "
            "WHERE budget_id = ?", (self.budget_id,),
        ).fetchone()
        if row is None:
            return self.limit_usd, self.limit_tokens, dict(self.per_tag), self.allow_unpriced
        per_tag = {str(k): float(v) for k, v in json.loads(row["per_tag"] or "{}").items()}
        return row["limit_usd"], row["limit_tokens"], per_tag, bool(row["allow_unpriced"])

    # -- trace ------------------------------------------------------------------

    def _attach_trace(self, sink: TraceSink) -> None:
        """Route ``budget.expire`` events to *sink* (a Kernel attaches its tracer)."""
        with self._lock:
            if all(existing is not sink for existing in self._sinks):
                self._sinks.append(sink)

    def _detach_trace(self, sink: TraceSink) -> None:
        with self._lock:
            self._sinks = [existing for existing in self._sinks if existing is not sink]

    def _emit(self, event: dict[str, Any]) -> None:
        for sink in list(self._sinks):
            safe_emit(sink, dict(event))

    # -- spend rows ---------------------------------------------------------------

    @staticmethod
    def _add_spend(
        conn: sqlite3.Connection, budget_id: str, key: str, value: str,
        usd: float, tokens: int,
    ) -> None:
        conn.execute(
            "INSERT INTO budget_spend (budget_id, scope_key, scope_value, spent_usd, spent_tokens) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(budget_id, scope_key, scope_value) DO UPDATE SET "
            "spent_usd = spent_usd + excluded.spent_usd, "
            "spent_tokens = spent_tokens + excluded.spent_tokens",
            (budget_id, key, value, usd, tokens),
        )

    def _charge(
        self, conn: sqlite3.Connection, usd: float, tokens: int, slots: list[tuple[str, str]],
    ) -> None:
        """Add *usd*/*tokens* to the kernel scope and *usd* to each tag slot."""
        if usd or tokens:
            self._add_spend(conn, self.budget_id, *_KERNEL_SCOPE, usd, tokens)
        if usd:
            for key, value in slots:
                self._add_spend(conn, self.budget_id, key, value, usd, 0)

    def _expire(self, conn: sqlite3.Connection, now: int) -> list[dict[str, Any]]:
        """Charge open reservations whose lease passed (inside a write transaction)."""
        rows = conn.execute(
            "SELECT id, call_id, usd, tokens, slots, holder FROM budget_reservations "
            "WHERE budget_id = ? AND state = 'open' AND lease_expires_ms <= ?",
            (self.budget_id, now),
        ).fetchall()
        events = []
        for row in rows:
            usd, tokens = float(row["usd"]), int(row["tokens"])
            self._charge(conn, usd, tokens, _slots_from_json(row["slots"]))
            conn.execute(
                "UPDATE budget_reservations SET state = 'expired', usd = 0, tokens = 0, "
                "expired_charge_usd = ?, expired_charge_tokens = ? WHERE id = ?",
                (usd, tokens, row["id"]),
            )
            events.append({
                "event": "budget.expire",
                "budget_id": self.budget_id,
                "call_id": row["call_id"],
                "charged_usd": usd,
                "charged_tokens": tokens,
                "usd": usd,
                "tokens": tokens,
                "holder": row["holder"],
            })
        return events

    def _totals(
        self, conn: sqlite3.Connection, now: int, slot: tuple[str, str] | None = None,
    ) -> tuple[float, int, float, int]:
        """``(spent_usd, spent_tokens, reserved_usd, reserved_tokens)`` for one scope.

        Open reservations past their lease count as spent even before a writer has
        marked them expired, so a read never shows a lapsed lease as reserved.
        """
        key, value = slot if slot is not None else _KERNEL_SCOPE
        row = conn.execute(
            "SELECT spent_usd, spent_tokens FROM budget_spend "
            "WHERE budget_id = ? AND scope_key = ? AND scope_value = ?",
            (self.budget_id, key, value),
        ).fetchone()
        spent_usd = float(row["spent_usd"]) if row else 0.0
        spent_tokens = int(row["spent_tokens"]) if row else 0
        reserved_usd = 0.0
        reserved_tokens = 0
        for res in conn.execute(
            "SELECT usd, tokens, slots, lease_expires_ms FROM budget_reservations "
            "WHERE budget_id = ? AND state = 'open'", (self.budget_id,),
        ):
            if slot is not None and slot not in _slots_from_json(res["slots"]):
                continue
            lapsed = int(res["lease_expires_ms"]) <= now
            if lapsed:
                spent_usd += float(res["usd"])
                if slot is None:
                    spent_tokens += int(res["tokens"])
            else:
                reserved_usd += float(res["usd"])
                if slot is None:
                    reserved_tokens += int(res["tokens"])
        return spent_usd, spent_tokens, reserved_usd, reserved_tokens

    # -- pending writes -----------------------------------------------------------------

    def _replay(self) -> None:
        """Write queued settles/releases in order; stop at the first that still fails."""
        while self._pending:
            op = self._pending[0]
            try:
                self._apply(op)
            except sqlite3.Error as exc:
                logger.debug("budget {}: pending {} still not written: {}",
                             self.budget_id, op.kind, exc)
                return
            self._pending.pop(0)

    def _apply(self, op: _Op) -> None:
        conn = self._begin()
        events: list[dict[str, Any]] = []
        try:
            events = self._expire(conn, _now_ms())
            if op.kind == "settle":
                self._apply_settle(conn, op)
            else:
                self._apply_release(conn, op.reservation)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        for event in events:
            self._emit(event)

    def _row(self, conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, usd, tokens, slots, state, unpriced, expired_charge_usd, "
            "expired_charge_tokens FROM budget_reservations WHERE id = ?", (row_id,),
        ).fetchone()

    def _apply_settle(self, conn: sqlite3.Connection, op: _Op) -> None:
        row = self._row(conn, op.reservation.row_id)
        if row is None:
            return
        slots = _slots_from_json(row["slots"])
        tokens, cost = op.tokens, op.cost
        state = row["state"]
        if state == "expired":
            # The expiry already charged the remainder: only the excess is new spend.
            charge_usd = float(row["expired_charge_usd"])
            charge_tokens = int(row["expired_charge_tokens"])
            take_tokens = min(charge_tokens, tokens)
            take_usd = min(charge_usd, cost) if cost is not None else 0.0
            extra_usd = (cost - take_usd) if cost is not None else 0.0
            self._charge(conn, 0.0, tokens - take_tokens, slots)
            if extra_usd:
                self._charge(conn, extra_usd, 0, slots)
            conn.execute(
                "UPDATE budget_reservations SET expired_charge_usd = ?, "
                "expired_charge_tokens = ?, unpriced = MAX(unpriced, ?) WHERE id = ?",
                (charge_usd - take_usd, charge_tokens - take_tokens, int(op.unpriced), row["id"]),
            )
            return
        # Open or released: CapBudget.settle.
        self._charge(conn, 0.0, tokens, slots)
        if cost is not None:
            self._charge(conn, cost, 0, slots)
        if state == "open":
            take_usd = min(float(row["usd"]), cost or 0.0)
            take_tokens = min(int(row["tokens"]), tokens)
            conn.execute(
                "UPDATE budget_reservations SET usd = usd - ?, tokens = tokens - ?, "
                "unpriced = MAX(unpriced, ?) WHERE id = ?",
                (take_usd, take_tokens, int(op.unpriced), row["id"]),
            )
        elif op.unpriced:
            conn.execute(
                "UPDATE budget_reservations SET unpriced = 1 WHERE id = ?", (row["id"],),
            )

    def _apply_release(self, conn: sqlite3.Connection, reservation: SharedReservation) -> None:
        row = self._row(conn, reservation.row_id)
        if row is None or row["state"] == "released":
            return
        slots = _slots_from_json(row["slots"])
        if row["state"] == "expired":
            # Without the expiry the remaining tokens would have been handed back, and
            # so would the remaining USD unless the attempt was unpriced (Q1).
            charge_usd = 0.0 if row["unpriced"] else float(row["expired_charge_usd"])
            charge_tokens = int(row["expired_charge_tokens"])
            if charge_usd or charge_tokens:
                self._charge(conn, -charge_usd, -charge_tokens, slots)
            conn.execute(
                "UPDATE budget_reservations SET state = 'released', expired_charge_usd = 0, "
                "expired_charge_tokens = 0 WHERE id = ?", (row["id"],),
            )
            return
        usd = float(row["usd"])
        if row["unpriced"] and usd:
            # Tokens were billed at an unknown price: keep the reservation as spend.
            self._charge(conn, usd, 0, slots)
        conn.execute(
            "UPDATE budget_reservations SET state = 'released', usd = 0, tokens = 0 WHERE id = ?",
            (row["id"],),
        )

    def _queue(self, op: _Op) -> None:
        self._pending.append(op)
        self._replay()

    # -- the Budget protocol ---------------------------------------------------------------

    def admit(self, estimate: CallEstimate) -> SharedReservation:
        with self._lock:
            self._replay()
            now = _now_ms()
            try:
                conn = self._begin()
            except sqlite3.Error as exc:
                raise self._unavailable(estimate, exc) from exc
            events: list[dict[str, Any]] = []
            try:
                events = self._expire(conn, now)
                limit_usd, limit_tokens, per_tag, allow_unpriced = self._caps(conn)
                spent_usd, spent_tokens, reserved_usd, reserved_tokens = self._totals(conn, now)

                def tag_exposure(slot: Slot) -> float:
                    s_usd, _t, r_usd, _r = self._totals(conn, now, (slot[0], _slot_text(slot[1])))
                    return s_usd + r_usd

                amount, tokens, slots = check_admission(
                    limit_usd=limit_usd, limit_tokens=limit_tokens, per_tag=per_tag,
                    allow_unpriced=allow_unpriced, estimate=estimate,
                    exposure_usd=spent_usd + reserved_usd,
                    used_tokens=spent_tokens + reserved_tokens,
                    tag_exposure=tag_exposure,
                )
                cursor = conn.execute(
                    "INSERT INTO budget_reservations (budget_id, call_id, usd, tokens, slots, "
                    "holder, created_ms, lease_expires_ms, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                    (self.budget_id, estimate.call_id, amount, tokens, _slots_json(slots),
                     self._holder, now, now + int(self.lease_s * 1000)),
                )
                conn.execute("COMMIT")
            except BudgetExceeded:
                # Refused: keep the expiry marks (they are facts), add no reservation.
                try:
                    conn.execute("COMMIT")
                except sqlite3.Error:
                    conn.execute("ROLLBACK")
                    events = []
                for event in events:
                    self._emit(event)
                raise
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise self._unavailable(estimate, exc) from exc
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            for event in events:
                self._emit(event)
            return SharedReservation(
                row_id=int(cursor.lastrowid or 0), budget_id=self.budget_id,
                call_id=estimate.call_id, usd=amount, tokens=tokens, slots=slots,
            )

    def _unavailable(self, estimate: CallEstimate, exc: BaseException) -> BudgetExceeded:
        detail = "the budget database is locked" if isinstance(exc, sqlite3.Error) and \
            _is_locked(exc) else f"the budget database failed: {exc}"
        return refusal(
            estimate,
            f"{detail} (no write lock within {self.lock_timeout_s:g}s); refusing to admit "
            "unmetered spend",
            "budget_unavailable",
        )

    def settle(self, reservation: SharedReservation, event: LedgerEvent) -> None:
        tokens, cost, unpriced = settle_charge(event)
        with self._lock:
            self._queue(_Op("settle", reservation, tokens=tokens, cost=cost, unpriced=unpriced))

    def release(self, reservation: SharedReservation) -> None:
        with self._lock:
            if reservation.released:
                return
            reservation.released = True
            self._queue(_Op("release", reservation))

    def flush(self, timeout_s: float | None = None) -> bool:
        """Write pending settles/releases (retrying until *timeout_s*, default ``lock_timeout_s``).

        Returns True when nothing is left pending. ``Kernel.close()`` calls this.
        """
        deadline = time.monotonic() + (self.lock_timeout_s if timeout_s is None else timeout_s)
        with self._lock:
            while True:
                self._replay()
                if not self._pending or time.monotonic() >= deadline:
                    return not self._pending
                time.sleep(0.05)

    @property
    def pending_writes(self) -> int:
        """Settles/releases not yet written (their reservations stay counted)."""
        with self._lock:
            return len(self._pending)

    def close(self) -> None:
        """Flush, then close this object's connection (it reopens on use)."""
        self.flush()
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    # -- figures ----------------------------------------------------------------------

    def _read(self, slot: tuple[str, str] | None = None) -> tuple[float, int, float, int]:
        with self._lock:
            self._replay()
            conn = self._connection()
            now = _now_ms()
            lapsed = conn.execute(
                "SELECT 1 FROM budget_reservations WHERE budget_id = ? AND state = 'open' "
                "AND lease_expires_ms <= ? LIMIT 1", (self.budget_id, now),
            ).fetchone()
            if lapsed is not None:
                self._try_expire(conn, now)
            # One read transaction: a consistent snapshot of spend and reservations.
            conn.execute("BEGIN")
            try:
                return self._totals(conn, now, slot)
            finally:
                conn.execute("COMMIT")

    def _try_expire(self, conn: sqlite3.Connection, now: int) -> None:
        """Mark lapsed leases (emitting ``budget.expire``) if the write lock is free soon.

        Never needed for correct figures (``_totals`` counts lapsed leases as spent), so
        it waits only briefly for the lock.
        """
        events: list[dict[str, Any]] = []
        conn.execute("PRAGMA busy_timeout = 100")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                events = self._expire(conn, now)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                events = []
                raise
        except sqlite3.Error:
            events = []
        finally:
            conn.execute(f"PRAGMA busy_timeout = {int(self.lock_timeout_s * 1000)}")
        for event in events:
            self._emit(event)

    def snapshot(self, tag: str | None = None, value: Any = None) -> dict[str, Any]:
        """The budget in force (``budget-event.v1``), as :meth:`CapBudget.snapshot`."""
        with self._lock:
            conn = self._connection()
            limit_usd, limit_tokens, per_tag, _allow = self._caps(conn)
        if tag is None:
            spent_usd, spent_tokens, reserved_usd, reserved_tokens = self._read()
            return budget_figures(
                cap_usd=limit_usd, cap_tokens=limit_tokens, spent_usd=spent_usd,
                spent_tokens=spent_tokens, reserved_usd=reserved_usd,
                reserved_tokens=reserved_tokens,
                scope={"kind": "kernel", "key": None, "value": None},
            )
        slot = _tag_slot(tag, value)
        spent_usd, _t, reserved_usd, _r = self._read((tag, _slot_text(slot[1])))
        return budget_figures(
            cap_usd=per_tag.get(tag), cap_tokens=None, spent_usd=spent_usd, spent_tokens=None,
            reserved_usd=reserved_usd, reserved_tokens=None,
            scope={"kind": "tag", "key": tag, "value": slot[1]},
        )

    @property
    def spent_usd(self) -> float:
        return self._read()[0]

    @property
    def spent_tokens(self) -> int:
        return self._read()[1]

    @property
    def reserved_usd(self) -> float:
        return self._read()[2]

    @property
    def reserved_tokens(self) -> int:
        return self._read()[3]

    def exposure(self, tag: str | None = None, value: Any = None) -> float:
        """Spent plus reserved USD: in total, or for one ``(tag, value)``."""
        if tag is None:
            spent, _t, reserved, _r = self._read()
        else:
            slot = _tag_slot(tag, value)
            spent, _t, reserved, _r = self._read((tag, _slot_text(slot[1])))
        return spent + reserved

    def __repr__(self) -> str:
        return (
            f"SharedCapBudget(data_dir={str(self.data_dir)!r}, budget_id={self.budget_id!r}, "
            f"limit_usd={self.limit_usd}, limit_tokens={self.limit_tokens}, "
            f"per_tag={dict(self.per_tag)})"
        )


__all__ = ["BUDGET_DDL", "SharedCapBudget", "SharedReservation"]
