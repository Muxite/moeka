"""SQLite persistence and chart queries for LLM usage records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nanobot.llm_usage.models import LLMCallRecord

SCHEMA_VERSION = 4
# Columns added after v1, as (name, declaration). v2 (Task 14): the kernel ledger's
# model tier and USD cost. v3 (spec 001): typed attribution and the rest of the
# ``usage-record.v1`` event. Migrations only ever ADD nullable columns, so an old row
# reads back with NULL for them and no data is rewritten.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tier", "TEXT"),  # v2
    ("cost_usd", "REAL"),  # v2
    ("call_id", "TEXT"),  # v3
    ("attempt", "INTEGER"),
    ("consumer", "TEXT"),
    ("agent", "TEXT"),
    ("session", "TEXT"),
    ("role", "TEXT"),
    ("purpose", "TEXT"),
    ("trace_id", "TEXT"),
    ("slot", "TEXT"),
    ("alias", "TEXT"),
    ("request_key", "TEXT"),
    ("prompt_version", "TEXT"),
    ("waste_label", "TEXT"),
    ("waste_set_by", "TEXT"),
    ("outcome", "TEXT"),
    ("price_source", "TEXT"),
    ("usage_source", "TEXT"),
    ("cost_billed", "INTEGER"),
    ("tokens_reasoning", "INTEGER"),
    ("tags", "TEXT"),  # v4: bounded host tags (JSON object), so the store round-trips the event
)
# Attribution and identity columns a usage event writes (see ``record_event``).
_EVENT_COLUMNS = (
    "call_id", "attempt", "consumer", "agent", "session", "role", "purpose", "trace_id",
    "slot", "alias", "request_key", "prompt_version", "waste_label", "waste_set_by",
    "outcome", "price_source", "usage_source", "cost_billed", "tokens_reasoning", "tags",
)
_EVENTS_DDL = """
    CREATE TABLE IF NOT EXISTS llm_usage_events (
        id INTEGER PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        kind TEXT NOT NULL,
        call_id TEXT,
        attempt INTEGER,
        consumer TEXT,
        agent TEXT,
        session TEXT,
        role TEXT,
        purpose TEXT,
        trace_id TEXT,
        model TEXT,
        provider TEXT,
        alias TEXT,
        saved_tokens_in INTEGER,
        saved_tokens_out INTEGER,
        saved_cost_usd REAL,
        waste_label TEXT,
        reason_code TEXT,
        payload TEXT,
        dedupe_key TEXT
    );
    CREATE INDEX IF NOT EXISTS llm_usage_events_call_idx ON llm_usage_events(call_id);
    CREATE INDEX IF NOT EXISTS llm_usage_events_kind_ts_idx ON llm_usage_events(kind, ts_ms);
"""
MAX_DAYS_RETAINED = 400
MAX_CALLS_RETAINED = 100_000
MAX_PAYLOAD_CHARS = 8000
_META_DDL = """
    CREATE TABLE IF NOT EXISTS llm_usage_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

_ERROR_KINDS = frozenset({
    "authentication",
    "cancelled",
    "configuration",
    "connection",
    "content_filter",
    "context_length",
    "empty",
    "http",
    "invalid_request",
    "overloaded",
    "permission",
    "rate_limit",
    "refusal",
    "server_error",
    "timeout",
})
_FINISH_REASONS = frozenset({
    "cancelled",
    "content_filter",
    "error",
    "function_call",
    "length",
    "refusal",
    "stop",
    "tool_calls",
})

_USAGE_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_read_observed_input_tokens",
    "cache_write_observed_input_tokens",
    "total_tokens",
    "reported_tokens",
    "estimated_tokens",
    "generation_ms",
    "measured_output_tokens",
    "ttft_ms",
    "timed_requests",
)
_REQUEST_COLUMNS = (
    "requests",
    "successful_requests",
    "failed_requests",
    "reported_requests",
    "estimated_requests",
)
_AGGREGATE_SQL = """
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(
        CASE WHEN cache_read_tokens IS NOT NULL THEN input_tokens ELSE 0 END
    ), 0) AS cache_read_observed_input_tokens,
    COALESCE(SUM(
        CASE WHEN cache_write_tokens IS NOT NULL THEN input_tokens ELSE 0 END
    ), 0) AS cache_write_observed_input_tokens,
    COALESCE(SUM(total_tokens), 0) AS total_tokens,
    COALESCE(SUM(reported_tokens), 0) AS reported_tokens,
    COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
    COALESCE(SUM(generation_ms), 0) AS generation_ms,
    COALESCE(SUM(measured_output_tokens), 0) AS measured_output_tokens,
    COALESCE(SUM(ttft_ms), 0) AS ttft_ms,
    COALESCE(SUM(timed_requests), 0) AS timed_requests,
    COUNT(*) AS requests,
    COALESCE(SUM(CASE WHEN finish_reason IN ('error', 'cancelled') THEN 0 ELSE 1 END), 0)
        AS successful_requests,
    COALESCE(SUM(CASE WHEN finish_reason IN ('error', 'cancelled') THEN 1 ELSE 0 END), 0)
        AS failed_requests,
    COALESCE(SUM(
        CASE WHEN total_tokens IS NOT NULL AND NOT (
            estimated_tokens > 0 AND reported_tokens = 0
        ) THEN 1 ELSE 0 END
    ), 0) AS reported_requests,
    COALESCE(SUM(
        CASE WHEN estimated_tokens > 0 AND reported_tokens = 0 THEN 1 ELSE 0 END
    ), 0) AS estimated_requests,
    COALESCE(SUM(duration_ms), 0) AS duration_ms
"""


def _zone(timezone_name: str | None) -> timezone | ZoneInfo:
    if not timezone_name:
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _clean_error_kind(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    return cleaned if cleaned in _ERROR_KINDS else "other"


def _clean_finish_reason(value: str) -> str:
    cleaned = value.strip().lower()
    return cleaned if cleaned in _FINISH_REASONS else "other"


def _clean_status_code(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _clean_cost(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _as_int_row(row: sqlite3.Row) -> dict[str, int]:
    return {
        key: max(0, int(row[key] or 0))
        for key in (*_USAGE_COLUMNS, *_REQUEST_COLUMNS, "duration_ms")
    }


def _empty_totals() -> dict[str, int]:
    return {key: 0 for key in (*_USAGE_COLUMNS, *_REQUEST_COLUMNS, "duration_ms")}


def _sum_rows(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = _empty_totals()
    for row in rows:
        for key in totals:
            totals[key] += max(0, int(row.get(key) or 0))
    return totals


class LLMUsageStore:
    """A small synchronous WAL database shared by gateway threads/processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._connection_pid: int | None = None
        self._last_prune_utc_day: int | None = None
        self._writes_since_size_prune = 0
        self._write_version = 0
        self._cached_payload_key: tuple[int, str, str, int, int] | None = None
        self._cached_payload: dict[str, Any] | None = None
        # Loss accounting (per process): a write that cannot land is kept for the next
        # write instead of vanishing, up to MAX_PENDING; beyond that it is dropped and counted.
        self._pending: deque[tuple[str, Any]] = deque()
        self._call_write_failures = 0
        self._write_dropped = 0
        self._duplicates = 0

    #: Writes kept for retry while the database is locked or unwritable.
    MAX_PENDING = 1000

    def _connect(self) -> sqlite3.Connection:
        pid = os.getpid()
        if self._connection is not None and self._connection_pid == pid:
            return self._connection
        if self._connection is not None:
            self._connection.close()
            self._cached_payload_key = None
            self._cached_payload = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=0.25,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.create_function("llm_usage_local_day", 2, self._local_day, deterministic=True)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                source TEXT NOT NULL,
                stream INTEGER NOT NULL,
                finish_reason TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reported_tokens INTEGER,
                estimated_tokens INTEGER,
                generation_ms INTEGER,
                measured_output_tokens INTEGER,
                ttft_ms INTEGER,
                timed_requests INTEGER,
                error_status_code INTEGER,
                error_kind TEXT,
                tier TEXT,
                cost_usd REAL
            );
            CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx
                ON llm_calls(started_at_ms);
            CREATE INDEX IF NOT EXISTS llm_calls_provider_model_time_idx
                ON llm_calls(provider, model, started_at_ms);
            """
        )
        old_version = self._migrate(connection)
        connection.executescript(_EVENTS_DDL)
        connection.executescript(_META_DDL)
        self._migrate_events(connection)
        if old_version == 3:
            self._fix_v3_retry_labels(connection)
        if old_version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection = connection
        self._connection_pid = pid
        return connection

    @staticmethod
    def _fix_v3_retry_labels(connection: sqlite3.Connection) -> None:
        """v3 labelled attempt n > 1 ``retry``; the waste is attempt n-1 (usage-record 1.1).

        Move each kernel-set label to the attempt before it as a linked waste event. Runs
        once, on the v3 -> v4 step, inside one transaction.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT OR IGNORE INTO llm_usage_events (ts_ms, kind, call_id, attempt, consumer, "
                "waste_label, payload, dedupe_key) "
                "SELECT started_at_ms, 'waste', call_id, attempt - 1, consumer, 'retry', "
                "'{\"waste_set_by\": \"kernel\", \"migrated_from\": \"v3\"}', "
                "'waste:' || call_id || ':' || (attempt - 1) || ':retry' FROM llm_calls "
                "WHERE waste_label = 'retry' AND waste_set_by = 'kernel' "
                "AND call_id IS NOT NULL AND attempt > 1"
            )
            connection.execute(
                "UPDATE llm_calls SET waste_label = NULL, waste_set_by = NULL "
                "WHERE waste_label = 'retry' AND waste_set_by = 'kernel'"
            )
            connection.execute("COMMIT")
        except sqlite3.Error:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> int:
        """Bring an older ``llm_calls`` table up to ``SCHEMA_VERSION`` in place; returns the
        ``user_version`` it found.

        Keyed on the actual columns (not only ``user_version``) so a half-applied
        or concurrent migration from another process converges instead of failing.
        """
        row = connection.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        existing = {str(info[1]) for info in connection.execute("PRAGMA table_info(llm_calls)")}
        for name, declaration in _ADDED_COLUMNS:
            if name in existing:
                continue
            try:
                connection.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        connection.execute(
            "CREATE INDEX IF NOT EXISTS llm_calls_call_idx ON llm_calls(call_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS llm_calls_consumer_time_idx "
            "ON llm_calls(consumer, started_at_ms)"
        )
        return version

    @staticmethod
    def _migrate_events(connection: sqlite3.Connection) -> None:
        """Idempotency: one row per call attempt, per cache hit and per waste label.

        ``llm_calls`` is unique on ``(call_id, attempt)`` and ``llm_usage_events`` on
        ``dedupe_key``; a duplicate delivery is ignored and counted. Databases written
        before these indexes may already hold duplicates: the index is then skipped
        (logged) rather than failing the open.
        """
        existing = {
            str(info[1]) for info in connection.execute("PRAGMA table_info(llm_usage_events)")
        }
        if "dedupe_key" not in existing:
            try:
                connection.execute("ALTER TABLE llm_usage_events ADD COLUMN dedupe_key TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS llm_usage_events_dedupe_uq "
            "ON llm_usage_events(dedupe_key) WHERE dedupe_key IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS llm_calls_attempt_uq "
            "ON llm_calls(call_id, attempt) WHERE call_id IS NOT NULL AND attempt IS NOT NULL",
        ):
            try:
                connection.execute(ddl)
            except sqlite3.IntegrityError:
                from loguru import logger

                logger.warning("usage store holds duplicate rows; idempotency index skipped")

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=0.25,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.create_function("llm_usage_local_day", 2, self._local_day, deterministic=True)
        return connection

    @staticmethod
    def _local_day(started_at_ms: object, timezone_name: object) -> str | None:
        if not isinstance(started_at_ms, int) or not isinstance(timezone_name, str):
            return None
        dt = datetime.fromtimestamp(started_at_ms / 1000, timezone.utc)
        return dt.astimezone(_zone(timezone_name)).date().isoformat()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._connection_pid = None
            self._cached_payload_key = None
            self._cached_payload = None

    def record_event(self, call: LLMCallRecord, event: Any) -> None:
        """Record *call* together with the ledger *event* it produced.

        The row's attribution and identity columns are projected from *event*, so the
        stored row and the ``model.call`` event are one fact (spec 001 FR-008).
        """
        self.record(call, event)

    def record(self, call: LLMCallRecord, event: Any = None) -> None:
        usage = call.usage
        usage_data = usage.to_dict() if usage is not None else {}
        values: tuple[object, ...] = (
            call.started_at_ms,
            call.duration_ms,
            call.provider[:120],
            call.model[:240],
            call.source,
            int(call.stream),
            _clean_finish_reason(call.finish_reason),
            *(
                usage_data.get(key)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reported_tokens",
                    "estimated_tokens",
                    "generation_ms",
                    "measured_output_tokens",
                    "ttft_ms",
                    "timed_requests",
                )
            ),
            _clean_status_code(call.error_status_code),
            _clean_error_kind(call.error_kind),
            call.tier[:32] if call.tier else None,
            _clean_cost(call.cost_usd),
        )
        columns = [
            "started_at_ms", "duration_ms", "provider", "model", "source", "stream",
            "finish_reason", "input_tokens", "output_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "reported_tokens",
            "estimated_tokens", "generation_ms", "measured_output_tokens",
            "ttft_ms", "timed_requests", "error_status_code", "error_kind",
            "tier", "cost_usd",
        ]
        if event is not None:
            values = (*values, *self._event_values(event))
            columns.extend(_EVENT_COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO llm_calls ({', '.join(columns)}) "  # noqa: S608 - fixed names
            f"VALUES ({', '.join('?' * len(columns))})"
        )
        with self._lock:
            try:
                connection = self._connect()
                self._insert(connection, sql, values)
                self._flush_pending(connection)
            except sqlite3.Error:
                self._call_write_failures += 1
                self._defer("call", (sql, values))
                raise
            self._prune_if_due(connection)

    def _insert(self, connection: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> None:
        cursor = connection.execute(sql, values)
        if cursor.rowcount == 0:
            self._duplicates += 1  # a duplicate delivery: already counted once
        self._write_version += 1
        self._cached_payload_key = None
        self._cached_payload = None

    def _defer(self, kind: str, item: Any) -> None:
        """Keep a write that could not land (the lock is held by the caller)."""
        self._pending.append((kind, item))
        while len(self._pending) > self.MAX_PENDING:
            self._pending.popleft()
            self._write_dropped += 1

    def _flush_pending(self, connection: sqlite3.Connection) -> None:
        """Retry deferred writes in order; stop at the first that fails again."""
        while self._pending:
            kind, item = self._pending[0]
            try:
                if kind == "call":
                    self._insert(connection, *item)
                else:
                    self._insert_event(connection, item)
            except sqlite3.Error:
                return
            self._pending.popleft()

    def stats(self) -> dict[str, Any]:
        """Loss and retention accounting for this store (durable counters survive restarts)."""
        with self._lock:
            connection = self._connect()
            meta = dict(connection.execute("SELECT key, value FROM llm_usage_meta").fetchall())
            oldest = connection.execute("SELECT MIN(started_at_ms) FROM llm_calls").fetchone()[0]
            return {
                "pending_writes": len(self._pending),
                "write_failures": self._call_write_failures,
                "write_dropped": self._write_dropped,
                "duplicates": self._duplicates,
                "pruned_rows": int(meta.get("pruned_rows", 0)),
                "pruned_through_id": int(meta.get("pruned_through_id", 0)),
                "oldest_started_at_ms": oldest,
            }

    @staticmethod
    def _event_values(event: Any) -> tuple[object, ...]:
        def text(name: str, limit: int = 240) -> str | None:
            value = getattr(event, name, None)
            return None if value is None else str(value)[:limit]

        billed = getattr(event, "cost_is_billed", None)
        reasoning = getattr(event, "tokens_reasoning", None)
        attempt = getattr(event, "attempt", None)
        tags = getattr(event, "tags", None)
        tags_json = None
        if tags:
            from nanobot.kernel.ledger import sanitize_tags

            tags_json = json.dumps(sanitize_tags(tags), sort_keys=True)
        return (
            text("call_id"),
            attempt if isinstance(attempt, int) else None,
            text("consumer"), text("agent"), text("session"), text("role"), text("purpose"),
            text("trace_id"), text("slot"), text("alias"), text("request_key"),
            text("prompt_version"), text("waste_label"), text("waste_set_by"),
            text("outcome"), text("price_source"), text("usage_source"),
            None if billed is None else int(bool(billed)),
            reasoning if isinstance(reasoning, int) else None,
            tags_json,
        )

    @staticmethod
    def _payload(event: dict[str, Any]) -> str:
        """The event as JSON, always parseable: an oversized event is slimmed (long values
        and nested objects dropped), never cut mid-document."""
        text = json.dumps(event, default=str, sort_keys=True)
        if len(text) <= MAX_PAYLOAD_CHARS:
            return text
        slim = {
            k: v for k, v in event.items()
            if isinstance(v, str | int | float | bool | type(None)) and len(str(v)) <= 512
        }
        slim["payload_slimmed"] = True
        return json.dumps(slim, default=str, sort_keys=True)[:MAX_PAYLOAD_CHARS]

    def record_usage_event(self, event: dict[str, Any]) -> None:
        """Append one non-call usage event: ``cache_hit``, ``refusal`` or ``waste``.

        *event* is the content-free trace payload (``cache.hit``, ``budget.refuse`` or
        ``call.waste``); only the attribution and figures are kept, plus the JSON payload.
        Idempotent per call: the same hit, refusal or label delivered twice lands once.
        """
        kind = {"cache.hit": "cache_hit", "budget.refuse": "refusal", "call.waste": "waste"}.get(
            str(event.get("event")),
        )
        if kind is None:
            raise ValueError(f"not a usage event: {event.get('event')!r}")

        def num(name: str, as_int: bool = True) -> object:
            value = event.get(name)
            if value is None or isinstance(value, bool):
                return None
            return int(value) if as_int else float(value)

        refusal = event.get("refusal")
        reason = refusal.get("code") if isinstance(refusal, dict) else None
        label = event.get("waste_label") or event.get("label")
        call_id = event.get("call_id")
        dedupe = None
        if call_id:
            dedupe = (
                f"waste:{call_id}:{event.get('attempt')}:{label}" if kind == "waste"
                else f"{kind}:{call_id}"
            )
        row = (
            int(event.get("started_at_ms") or time.time() * 1000), kind,
            call_id, event.get("attempt"),
            event.get("consumer"), event.get("agent"), event.get("session"), event.get("role"),
            event.get("purpose"), event.get("trace_id"), event.get("model"),
            event.get("provider"), event.get("alias"),
            num("saved_tokens_in"), num("saved_tokens_out"), num("saved_cost_usd", False),
            label, reason, self._payload(event), dedupe,
        )
        with self._lock:
            try:
                connection = self._connect()
                self._insert_event(connection, row)
                self._flush_pending(connection)
            except sqlite3.Error:
                self._defer("event", row)
                raise

    def _insert_event(self, connection: sqlite3.Connection, row: tuple[object, ...]) -> None:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO llm_usage_events (ts_ms, kind, call_id, attempt, consumer, "
            "agent, session, role, purpose, trace_id, model, provider, alias, saved_tokens_in, "
            "saved_tokens_out, saved_cost_usd, waste_label, reason_code, payload, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        if cursor.rowcount == 0:
            self._duplicates += 1
        self._write_version += 1

    def _prune_if_due(self, connection: sqlite3.Connection) -> None:
        utc_day = int(time.time() // 86_400)
        self._writes_since_size_prune += 1
        prune_age = self._last_prune_utc_day != utc_day
        prune_size = self._writes_since_size_prune >= 1_024
        if not prune_age and not prune_size:
            return
        deleted = 0
        through = 0
        if prune_age:
            cutoff_ms = int(
                (datetime.now(timezone.utc) - timedelta(days=MAX_DAYS_RETAINED)).timestamp()
                * 1000
            )
            row = connection.execute(
                "SELECT MAX(id) FROM llm_calls WHERE started_at_ms < ?", (cutoff_ms,),
            ).fetchone()
            through = int(row[0] or 0)
            deleted += connection.execute(
                "DELETE FROM llm_calls WHERE started_at_ms < ?", (cutoff_ms,),
            ).rowcount
            connection.execute("DELETE FROM llm_usage_events WHERE ts_ms < ?", (cutoff_ms,))
        row = connection.execute(
            "SELECT id FROM llm_calls ORDER BY id DESC LIMIT 1 OFFSET ?", (MAX_CALLS_RETAINED,),
        ).fetchone()
        if row is not None:
            through = max(through, int(row[0]))
            deleted += connection.execute(
                "DELETE FROM llm_calls WHERE id <= ?", (int(row[0]),),
            ).rowcount
        connection.execute(
            "DELETE FROM llm_usage_events WHERE id <= COALESCE((SELECT id FROM llm_usage_events "
            "ORDER BY id DESC LIMIT 1 OFFSET ?), -1)", (MAX_CALLS_RETAINED,),
        )
        if deleted:
            connection.execute(
                "INSERT INTO llm_usage_meta (key, value) VALUES ('pruned_rows', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value", (deleted,),
            )
            connection.execute(
                "INSERT INTO llm_usage_meta (key, value) VALUES ('pruned_through_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)", (through,),
            )
        self._last_prune_utc_day = utc_day
        self._writes_since_size_prune = 0

    # -- tamper evidence -----------------------------------------------------------------

    _CHECK_CALLS = (
        "SELECT id, call_id, attempt, started_at_ms, provider, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, consumer, outcome, "
        "finish_reason FROM llm_calls WHERE id BETWEEN ? AND ? ORDER BY id"
    )
    _CHECK_EVENTS = (
        "SELECT id, kind, call_id, attempt, ts_ms, saved_tokens_in, saved_tokens_out, "
        "saved_cost_usd, waste_label, reason_code FROM llm_usage_events "
        "WHERE id BETWEEN ? AND ? ORDER BY id"
    )

    def _digest(self, connection: sqlite3.Connection, sql: str, lo: int, hi: int) -> tuple[int, str]:
        h = hashlib.sha256()
        n = 0
        for row in connection.execute(sql, (lo, hi)):
            h.update(json.dumps(list(tuple(row)), default=str).encode())
            h.update(b"\n")
            n += 1
        return n, h.hexdigest()

    def checkpoint(self) -> dict[str, Any]:
        """A digest of every row so far. Keep it where the agent cannot write (the harness's
        own state); :meth:`verify` later proves those rows were not edited, removed or
        reordered. Honest appends after the checkpoint do not affect it. Pruning does, so
        take one per run, not per year."""
        with self._lock:
            connection = self._connect()
            out: dict[str, Any] = {"at_ms": int(time.time() * 1000), "version": 1}
            for name, table, sql in (
                ("calls", "llm_calls", self._CHECK_CALLS),
                ("events", "llm_usage_events", self._CHECK_EVENTS),
            ):
                lo, hi = connection.execute(f"SELECT MIN(id), MAX(id) FROM {table}").fetchone()  # noqa: S608
                lo, hi = int(lo or 0), int(hi or 0)
                n, digest = self._digest(connection, sql, lo, hi)
                out[name] = {"first_id": lo, "last_id": hi, "rows": n, "digest": digest}
            return out

    def verify(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """Check the store against a :meth:`checkpoint`: ``{"ok", "reason"}``; the reason is
        ``rows_missing`` (deleted or pruned) or ``rows_changed`` (edited)."""
        with self._lock:
            connection = self._connect()
            for name, sql in (("calls", self._CHECK_CALLS), ("events", self._CHECK_EVENTS)):
                part = checkpoint[name]
                n, digest = self._digest(connection, sql, part["first_id"], part["last_id"])
                if n < part["rows"]:
                    return {"ok": False, "reason": "rows_missing", "table": name}
                if n != part["rows"] or digest != part["digest"]:
                    return {"ok": False, "reason": "rows_changed", "table": name}
        return {"ok": True, "reason": None}

    def count(self) -> int:
        with self._lock:
            row = self._connect().execute("SELECT COUNT(*) AS count FROM llm_calls").fetchone()
        return int(row["count"] if row is not None else 0)

    def _aggregate(
        self,
        *,
        connection: sqlite3.Connection,
        start_ms: int | None,
        end_ms: int,
        group_by: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        selected = f"{', '.join(group_by)}, " if group_by else ""
        where = "started_at_ms < ?"
        params: list[object] = [end_ms]
        if start_ms is not None:
            where = "started_at_ms >= ? AND started_at_ms < ?"
            params = [start_ms, end_ms]
        query = f"SELECT {selected}{_AGGREGATE_SQL} FROM llm_calls WHERE {where}"
        if group_by:
            query += f" GROUP BY {', '.join(group_by)} ORDER BY total_tokens DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return list(connection.execute(query, params).fetchall())

    def _daily_rows(
        self,
        *,
        connection: sqlite3.Connection,
        start_ms: int,
        end_ms: int,
        timezone_name: str,
    ) -> list[dict[str, Any]]:
        query = f"""
            SELECT llm_usage_local_day(started_at_ms, ?) AS date, source,
                   {_AGGREGATE_SQL}
            FROM llm_calls
            WHERE started_at_ms >= ? AND started_at_ms < ?
            GROUP BY date, source
            ORDER BY date, source
        """
        rows = connection.execute(
            query,
            (timezone_name, start_ms, end_ms),
        ).fetchall()
        by_date: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = cast(str | None, row["date"])
            if day is None:
                continue
            values = _as_int_row(row)
            aggregate = by_date.setdefault(
                day,
                {"date": day, **_empty_totals(), "sources": {}},
            )
            for key, value in values.items():
                aggregate[key] += value
            aggregate["sources"][str(row["source"])] = values
        return list(by_date.values())

    @staticmethod
    def _midnight_ms(value: date, zone: timezone | ZoneInfo) -> int:
        return int(datetime.combine(value, datetime.min.time(), tzinfo=zone).timestamp() * 1000)

    def usage_payload(
        self,
        *,
        days: int = 371,
        timezone_name: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        zone = _zone(timezone_name)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        today = current.astimezone(zone).date()
        safe_days = max(1, days)
        zone_name = getattr(zone, "key", "UTC")

        with self._lock:
            data_version_row = self._connect().execute("PRAGMA data_version").fetchone()
            data_version = int(data_version_row[0]) if data_version_row is not None else 0
            write_version = self._write_version
            cache_key = (
                safe_days,
                zone_name,
                today.isoformat(),
                write_version,
                data_version,
            )
            if self._cached_payload_key == cache_key and self._cached_payload is not None:
                return deepcopy(self._cached_payload)

        connection = self._read_connection()
        try:
            connection.execute("BEGIN")
            end_ms = self._midnight_ms(today + timedelta(days=1), zone)
            retained_start = today - timedelta(days=MAX_DAYS_RETAINED - 1)
            retained_start_ms = self._midnight_ms(retained_start, zone)
            daily = self._daily_rows(
                connection=connection,
                start_ms=retained_start_ms,
                end_ms=end_ms,
                timezone_name=zone_name,
            )

            requested_start = today - timedelta(days=safe_days - 1)
            visible_days = [row for row in daily if row["date"] >= requested_start.isoformat()]
            last_30_start_ms = self._midnight_ms(today - timedelta(days=29), zone)

            last_30_date = (today - timedelta(days=29)).isoformat()
            last_365_date = (today - timedelta(days=364)).isoformat()
            all_totals = _sum_rows(daily)
            totals_30 = _sum_rows(row for row in daily if row["date"] >= last_30_date)
            totals_365 = _sum_rows(row for row in daily if row["date"] >= last_365_date)

            provider_rows = self._aggregate(
                connection=connection,
                start_ms=last_30_start_ms,
                end_ms=end_ms,
                group_by=("provider", "model"),
                limit=50,
            )
            providers_30d = [
                {
                    "provider": str(row["provider"]),
                    "model": str(row["model"]),
                    **_as_int_row(row),
                }
                for row in provider_rows
            ]
            model_days_30d = [
                {
                    "date": str(row["date"]),
                    "provider": str(row["provider"]),
                    "model": str(row["model"]),
                    "total_tokens": int(row["total_tokens"]),
                }
                for row in connection.execute(
                    """
                    SELECT llm_usage_local_day(started_at_ms, ?) AS date, provider, model,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens
                    FROM llm_calls
                    WHERE started_at_ms >= ? AND started_at_ms < ?
                    GROUP BY date, provider, model
                    HAVING SUM(total_tokens) > 0
                    ORDER BY date, provider, model
                    """,
                    (zone_name, last_30_start_ms, end_ms),
                )
            ]

            active_dates = {
                date.fromisoformat(row["date"]) for row in daily if row["total_tokens"] > 0
            }
            current_streak = 0
            cursor = today
            while cursor in active_dates:
                current_streak += 1
                cursor -= timedelta(days=1)
            longest_streak = 0
            running_streak = 0
            previous: date | None = None
            for cursor in sorted(active_dates):
                running_streak = running_streak + 1 if previous == cursor - timedelta(days=1) else 1
                longest_streak = max(longest_streak, running_streak)
                previous = cursor

            latest = (
                connection
                .execute("SELECT MAX(started_at_ms) AS updated_at_ms FROM llm_calls")
                .fetchone()
            )
            updated_at_ms = int(latest["updated_at_ms"] or 0) if latest is not None else 0
            denominator = totals_30["cache_read_observed_input_tokens"]
            payload = {
                "days": visible_days,
                "total_tokens": all_totals["total_tokens"],
                "total_tokens_30d": totals_30["total_tokens"],
                "total_tokens_365d": totals_365["total_tokens"],
                "reported_tokens_30d": totals_30["reported_tokens"],
                "estimated_tokens_30d": totals_30["estimated_tokens"],
                "cache_read_tokens_30d": totals_30["cache_read_tokens"],
                "cache_read_observed_input_tokens_30d": denominator,
                "cache_read_rate_30d": (
                    totals_30["cache_read_tokens"] / denominator if denominator else None
                ),
                "peak_day_tokens": max(
                    (int(row["total_tokens"]) for row in daily),
                    default=0,
                ),
                "current_streak_days": current_streak,
                "longest_streak_days": longest_streak,
                "active_days_30d": sum(
                    1
                    for row in daily
                    if row["date"] >= last_30_date and row["total_tokens"] > 0
                ),
                "requests_30d": totals_30["requests"],
                "failed_requests_30d": totals_30["failed_requests"],
                "providers_30d": providers_30d,
                "model_days_30d": model_days_30d,
                "updated_at": (
                    datetime.fromtimestamp(updated_at_ms / 1000, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if updated_at_ms
                    else None
                ),
            }
        finally:
            connection.close()

        with self._lock:
            latest_data_version_row = self._connect().execute("PRAGMA data_version").fetchone()
            latest_data_version = (
                int(latest_data_version_row[0])
                if latest_data_version_row is not None
                else 0
            )
            if self._write_version == write_version and latest_data_version == data_version:
                self._cached_payload_key = cache_key
                self._cached_payload = payload
        return deepcopy(payload)

    def recent_calls(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded metadata rows for diagnostics; never returns content."""
        safe_limit = min(max(1, limit), 1_000)
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    """
                SELECT * FROM llm_calls ORDER BY started_at_ms DESC, id DESC LIMIT ?
                """,
                    (safe_limit,),
                )
                .fetchall()
            )
        return [dict(row) for row in rows]

    def record_many(self, calls: Iterable[LLMCallRecord]) -> None:
        for call in calls:
            self.record(call)
