"""Read side of the usage store: filters, totals, group-by and ``usage-record.v1`` rows.

Pure SQL over ``llm_calls`` (physical attempts) and ``llm_usage_events`` (cache hits,
refusals, late waste labels). Content-free by construction: neither table holds text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.llm_usage.store import LLMUsageStore

SCHEMA_VERSION = "1.1"  # usage-record.v1
REQUEST_KEY_SCHEME = "moeka.request_key.v1"

_SHARED = ("consumer", "agent", "session", "role", "purpose", "model", "provider", "trace_id")
GROUP_BY_CALLS = (*_SHARED, "tier", "outcome", "waste_label", "alias", "source")
GROUP_BY_EVENTS = _SHARED


@dataclass(frozen=True, slots=True)
class UsageFilter:
    """Which usage to read. ``None`` fields do not filter; time is epoch milliseconds,
    ``since_ms`` inclusive and ``until_ms`` exclusive."""

    consumer: str | None = None
    agent: str | None = None
    session: str | None = None
    role: str | None = None
    purpose: str | None = None
    model: str | None = None
    provider: str | None = None
    trace_id: str | None = None
    call_id: str | None = None
    since_ms: int | None = None
    until_ms: int | None = None

    def clauses(self, time_column: str) -> tuple[str, list[object]]:
        parts: list[str] = []
        params: list[object] = []
        for name in (*_SHARED, "call_id"):
            value = getattr(self, name)
            if value is not None:
                parts.append(f"{name} = ?")
                params.append(value)
        if self.since_ms is not None:
            parts.append(f"{time_column} >= ?")
            params.append(self.since_ms)
        if self.until_ms is not None:
            parts.append(f"{time_column} < ?")
            params.append(self.until_ms)
        return (" AND ".join(parts) if parts else "1=1"), params


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Sums over one group of calls. ``tokens_in`` includes cache reads and writes.

    ``requests`` counts physical attempts (``model_call`` records); ``calls`` the logical
    calls they belong to (distinct ``call_id``); ``retries`` the attempts after a call's first;
    ``latency_ms`` sums attempt latencies (concurrent attempts overlap, so it is not wall time).
    ``cost_usd`` sums the priced attempts only and splits exactly into ``billed_cost_usd``
    (provider-reported usage at a known price, or the local-zero convention) and
    ``estimated_cost_usd`` (the producer's own count at its price table: a hint, not a bill);
    ``unpriced_requests`` counts the rest (their cost is unknown, never zero). ``wasted_*`` sum attempts carrying a waste
    label (set on the event or by a later ``call.waste`` event).
    """

    group: dict[str, Any]
    requests: int = 0
    calls: int = 0
    retries: int = 0
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    unpriced_requests: int = 0
    estimated_requests: int = 0
    estimated_tokens: int = 0
    failed_requests: int = 0
    wasted_tokens: int = 0
    wasted_cost_usd: float = 0.0
    cache_hits: int = 0
    saved_tokens_in: int = 0
    saved_tokens_out: int = 0
    saved_cost_usd: float = 0.0
    refusals: int = 0
    schema_version: str = SCHEMA_VERSION

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_CALL_SQL = """
    COUNT(*) AS requests,
    COUNT(DISTINCT COALESCE(call_id, 'legacy-' || id)) AS calls,
    COALESCE(SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END), 0) AS retries,
    COALESCE(SUM(duration_ms), 0.0) AS latency_ms,
    COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS tokens_in,
    COALESCE(SUM(COALESCE(output_tokens, 0)), 0) AS tokens_out,
    COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) AS cache_read_tokens,
    COALESCE(SUM(COALESCE(cache_write_tokens, 0)), 0) AS cache_write_tokens,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
    COALESCE(SUM(CASE WHEN cost_billed = 1 THEN cost_usd ELSE 0 END), 0.0) AS billed_cost_usd,
    COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL AND COALESCE(cost_billed, 0) = 0
        THEN cost_usd ELSE 0 END), 0.0) AS estimated_cost_usd,
    COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), 0) AS unpriced_requests,
    COALESCE(SUM(CASE WHEN usage_source IN ('estimated', 'mixed')
        OR (usage_source IS NULL AND estimated_tokens > 0 AND reported_tokens = 0)
        THEN 1 ELSE 0 END), 0) AS estimated_requests,
    COALESCE(SUM(CASE WHEN usage_source IN ('estimated', 'mixed')
        OR (usage_source IS NULL AND estimated_tokens > 0 AND reported_tokens = 0)
        THEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) ELSE 0 END), 0)
        AS estimated_tokens,
    COALESCE(SUM(CASE WHEN COALESCE(outcome, CASE WHEN finish_reason IN ('error', 'cancelled')
        THEN 'error' ELSE 'ok' END) IN ('error', 'timeout', 'cancelled') THEN 1 ELSE 0 END), 0)
        AS failed_requests,
    COALESCE(SUM(CASE WHEN waste_label IS NOT NULL
        THEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) ELSE 0 END), 0)
        AS wasted_tokens,
    COALESCE(SUM(CASE WHEN waste_label IS NOT NULL THEN cost_usd ELSE 0 END), 0.0)
        AS wasted_cost_usd
"""
# attempts with the late ``call.waste`` label folded in. A label names one attempt, or the
# whole call when its ``attempt`` is null; an attempt's own label wins over a call-wide one.
_LATE = """(
        SELECT {col} FROM llm_usage_events w
        WHERE w.kind = 'waste' AND w.call_id = c.call_id AND {match}
        ORDER BY w.id DESC LIMIT 1)"""
_CALLS_VIEW = f"""(
    SELECT c.*,
    COALESCE(c.waste_label,
        {_LATE.format(col="w.waste_label", match="w.attempt = c.attempt")},
        {_LATE.format(col="w.waste_label", match="w.attempt IS NULL")}) AS eff_waste,
    COALESCE(c.waste_set_by,
        {_LATE.format(col="json_extract(w.payload, '$.waste_set_by')", match="w.attempt = c.attempt")},
        {_LATE.format(col="json_extract(w.payload, '$.waste_set_by')", match="w.attempt IS NULL")},
        'caller') AS eff_set_by
    FROM llm_calls c
)"""


def _check_group(group_by: tuple[str, ...]) -> None:
    for name in group_by:
        if name not in GROUP_BY_CALLS:
            raise ValueError(f"cannot group usage by {name!r}; choose from {GROUP_BY_CALLS}")


def totals(
    store: LLMUsageStore,
    flt: UsageFilter | None = None,
    group_by: tuple[str, ...] = (),
) -> list[UsageTotals]:
    """Totals per group (one entry with an empty group when *group_by* is empty).

    Read in one transaction, so a query sees a consistent snapshot while writes continue.
    """
    flt = flt or UsageFilter()
    _check_group(group_by)
    call_where, call_params = flt.clauses("started_at_ms")
    event_where, event_params = flt.clauses("ts_ms")
    sel = {name: ("eff_waste" if name == "waste_label" else name) for name in group_by}
    call_select = "".join(f"{sel[g]} AS {g}, " for g in group_by)
    call_sql = (
        f"SELECT {call_select}{_CALL_SQL} FROM {_CALLS_VIEW} WHERE {call_where}"  # noqa: S608
    ).replace("waste_label IS NOT NULL", "eff_waste IS NOT NULL")
    if group_by:
        call_sql += " GROUP BY " + ", ".join(sel[g] for g in group_by)
    event_cols = [g for g in group_by if g in GROUP_BY_EVENTS]
    event_select = "".join(f"{g}, " for g in event_cols)
    event_sql = (
        f"SELECT {event_select}kind, COUNT(*) AS n, "
        "COALESCE(SUM(saved_tokens_in), 0) AS si, COALESCE(SUM(saved_tokens_out), 0) AS so, "
        "COALESCE(SUM(saved_cost_usd), 0.0) AS sc "
        f"FROM llm_usage_events WHERE kind IN ('cache_hit', 'refusal') AND {event_where} "  # noqa: S608
        f"GROUP BY {event_select}kind"
    )
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    with store._lock:  # noqa: SLF001 - the store owns its connection
        connection = store._connect()  # noqa: SLF001
        connection.execute("BEGIN")
        try:
            call_rows = connection.execute(call_sql, call_params).fetchall()
            event_rows = connection.execute(event_sql, event_params).fetchall()
        finally:
            connection.execute("COMMIT")
    for row in call_rows:
        key = tuple(row[g] for g in group_by)
        out[key] = {k: row[k] for k in row.keys() if k not in group_by}  # noqa: SIM118
    for row in event_rows:
        values = {c: row[c] for c in event_cols}
        key = tuple(values.get(g) for g in group_by)
        acc = out.setdefault(key, {})
        if row["kind"] == "cache_hit":
            acc["cache_hits"] = acc.get("cache_hits", 0) + row["n"]
            acc["saved_tokens_in"] = acc.get("saved_tokens_in", 0) + row["si"]
            acc["saved_tokens_out"] = acc.get("saved_tokens_out", 0) + row["so"]
            acc["saved_cost_usd"] = acc.get("saved_cost_usd", 0.0) + row["sc"]
        else:
            acc["refusals"] = acc.get("refusals", 0) + row["n"]
    if not out and not group_by:
        out[()] = {}
    result = [
        UsageTotals(group=dict(zip(group_by, key, strict=True)), **values)
        for key, values in out.items()
    ]
    result.sort(key=lambda t: (-t.tokens, str(sorted(t.group.items()))))
    return result


def _tags(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _record_from_call(row: sqlite3.Row) -> dict[str, Any]:
    attempt = row["attempt"] if row["attempt"] is not None else 1
    call_id = row["call_id"] or f"legacy-{row['id']}"
    cost = row["cost_usd"]
    failed = row["finish_reason"] in ("error", "cancelled")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": f"{call_id}:{attempt}",
        "kind": "model_call",
        "call_id": call_id,
        "attempt": attempt,
        "consumer": row["consumer"] or "unattributed",
        "agent": row["agent"], "session": row["session"], "role": row["role"],
        "purpose": row["purpose"], "slot": row["slot"], "source": row["source"],
        "tags": _tags(row["tags"]),
        "trace_id": row["trace_id"], "request_key": row["request_key"],
        "key_scheme": REQUEST_KEY_SCHEME if row["request_key"] else None,
        "prompt_version": row["prompt_version"],
        "started_at_ms": row["started_at_ms"], "latency_ms": row["duration_ms"],
        "model": row["model"], "provider": row["provider"], "alias": row["alias"],
        "tier": row["tier"],
        "tokens_in": row["input_tokens"] or 0, "tokens_out": row["output_tokens"] or 0,
        "tokens_cache_read": row["cache_read_tokens"],
        "tokens_cache_write": row["cache_write_tokens"],
        "tokens_reasoning": row["tokens_reasoning"],
        "usage_source": row["usage_source"] or "none",
        "cost_usd": cost,
        "cost_billed": bool(row["cost_billed"]) if row["cost_billed"] is not None else False,
        "price_source": row["price_source"] or ("none" if cost is None else "price_table"),
        "cache_hit": False,
        "saved_tokens_in": None, "saved_tokens_out": None, "saved_cost_usd": None,
        "finish_reason": row["finish_reason"],
        "outcome": row["outcome"] or ("error" if failed else "ok"),
        "error_kind": row["error_kind"],
        "waste_label": row["eff_waste"],
        "waste_set_by": row["eff_set_by"] if row["eff_waste"] else None,
        "producer": {"name": "moeka", "version": None},
    }


def _record_from_hit(row: sqlite3.Row) -> dict[str, Any]:
    call_id = row["call_id"] or f"hit-{row['id']}"
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return {
        "request_key": payload.get("request_key"),
        "key_scheme": payload.get("key_scheme"),
        "prompt_version": payload.get("prompt_version"),
        "schema_version": SCHEMA_VERSION,
        "record_id": f"{call_id}:0",
        "kind": "cache_hit", "call_id": call_id, "attempt": 0,
        "consumer": row["consumer"] or "unattributed",
        "agent": row["agent"], "session": row["session"], "role": row["role"],
        "purpose": row["purpose"], "trace_id": row["trace_id"],
        "started_at_ms": row["ts_ms"],
        "model": row["model"] or "unknown", "provider": row["provider"] or "unknown",
        "alias": row["alias"],
        "tokens_in": 0, "tokens_out": 0, "usage_source": "none",
        "cost_usd": 0.0, "cost_billed": True, "price_source": "cache",
        "cache_hit": True,
        "saved_tokens_in": row["saved_tokens_in"], "saved_tokens_out": row["saved_tokens_out"],
        "saved_cost_usd": row["saved_cost_usd"],
        "outcome": "ok", "tags": payload.get("tags") or {},
        "producer": {"name": "moeka", "version": None},
    }


def records(
    store: LLMUsageStore, flt: UsageFilter | None = None, *, limit: int = 100,
) -> list[dict[str, Any]]:
    """The newest *limit* ``usage-record.v1`` documents matching *flt* (calls and cache hits)."""
    flt = flt or UsageFilter()
    limit = max(0, int(limit))
    call_where, call_params = flt.clauses("started_at_ms")
    event_where, event_params = flt.clauses("ts_ms")
    with store._lock:  # noqa: SLF001
        connection = store._connect()  # noqa: SLF001
        calls = connection.execute(
            f"SELECT * FROM {_CALLS_VIEW} WHERE {call_where} "  # noqa: S608
            "ORDER BY started_at_ms DESC, id DESC LIMIT ?", [*call_params, limit],
        ).fetchall()
        hits = connection.execute(
            f"SELECT * FROM llm_usage_events WHERE kind = 'cache_hit' AND {event_where} "  # noqa: S608
            "ORDER BY ts_ms DESC, id DESC LIMIT ?", [*event_params, limit],
        ).fetchall()
    merged = [_record_from_call(r) for r in calls] + [_record_from_hit(r) for r in hits]
    merged.sort(key=lambda r: r["started_at_ms"] or 0, reverse=True)
    return merged[:limit]


_GROUPABLE = (*_SHARED, "tier", "outcome", "waste_label", "alias", "source")


def reduce_records(
    docs: Iterable[dict[str, Any]], group_by: tuple[str, ...] = (),
) -> list[UsageTotals]:
    """The reference reducer: :class:`UsageTotals` computed from ``usage-record`` documents
    alone (no store), so any producer's records can be merged in one analysis with the same
    definitions. A document delivered twice counts once (key: producer name and ``record_id``);
    cache hits add to ``cache_hits`` and ``saved_*`` and never to ``requests``. ``refusals`` come
    from budget events, not records, and stay 0 here."""
    seen: set[tuple[Any, Any]] = set()
    calls: dict[tuple[Any, ...], set[Any]] = {}
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for doc in docs:
        producer = (doc.get("producer") or {}).get("name")
        ident = (producer, doc.get("record_id"))
        if ident in seen:
            continue
        seen.add(ident)
        key = tuple(doc.get(g) for g in group_by)
        acc = out.setdefault(key, {})

        def add(name: str, value: float, acc: dict[str, Any] = acc) -> None:
            acc[name] = acc.get(name, 0) + value

        if doc.get("kind") == "cache_hit":
            add("cache_hits", 1)
            add("saved_tokens_in", doc.get("saved_tokens_in") or 0)
            add("saved_tokens_out", doc.get("saved_tokens_out") or 0)
            add("saved_cost_usd", doc.get("saved_cost_usd") or 0.0)
            continue
        cost = doc.get("cost_usd")
        add("requests", 1)
        calls.setdefault(key, set()).add((producer, doc.get("call_id")))
        add("retries", 1 if (doc.get("attempt") or 0) > 1 else 0)
        add("latency_ms", doc.get("latency_ms") or 0.0)
        add("tokens_in", doc.get("tokens_in") or 0)
        add("tokens_out", doc.get("tokens_out") or 0)
        add("cache_read_tokens", doc.get("tokens_cache_read") or 0)
        add("cache_write_tokens", doc.get("tokens_cache_write") or 0)
        add("cost_usd", cost or 0.0)
        add("billed_cost_usd", (cost or 0.0) if doc.get("cost_billed") else 0.0)
        add("estimated_cost_usd", cost if cost is not None and not doc.get("cost_billed") else 0.0)
        add("unpriced_requests", 1 if cost is None else 0)
        est = doc.get("usage_source") in ("estimated", "mixed")
        add("estimated_requests", 1 if est else 0)
        add("estimated_tokens", (doc.get("tokens_in") or 0) + (doc.get("tokens_out") or 0) if est else 0)
        add("failed_requests", 1 if doc.get("outcome") in ("error", "timeout", "cancelled") else 0)
        if doc.get("waste_label"):
            add("wasted_tokens", (doc.get("tokens_in") or 0) + (doc.get("tokens_out") or 0))
            add("wasted_cost_usd", cost or 0.0)
    result = []
    for key, values in out.items():
        values["calls"] = len(calls.get(key, ()))
        result.append(UsageTotals(group=dict(zip(group_by, key, strict=True)), **values))
    if not result and not group_by:
        result.append(UsageTotals(group={}))
    result.sort(key=lambda t: (-t.tokens, str(sorted(t.group.items()))))
    return result
