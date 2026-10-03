"""Helpers for the 009 held-out tests: the Terms fingerprint reference, fake keys, a no-network
OpenAI-compatible provider, call-record builders and the pre-009 store DDL."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_NAME = "llm_usage.sqlite3"
FP_RE = re.compile(r"^[0-9a-f]{12}$")
FP_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")

# Fake keys only. Upper-case letters and digits outside a-f keep them from colliding with
# lower-case hex fingerprints by chance.
KEY_MOEKA = "sk-or-test-AAAAMOEKAKEYQWERTYUIOPZXCVBNM0123456789"
KEY_AWORK = "sk-or-test-BBBBAWORKKEYPLMOKNIJBUHVYGTCRXZQ9876543210"
KEY_THIRD = "sk-or-test-CCCCTHIRDKEYJHGFSDLKZXMNVQPWOEIRUTY1357924"


def ref_fp(key: str | None) -> str | None:
    """Terms: first 12 lower-hex chars of sha256(key.strip()), ``None`` for no key."""
    if key is None:
        return None
    k = key.strip()
    if not k:
        return None
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:12]


def repo_root() -> Path:
    spec = importlib.util.find_spec("nanobot")
    assert spec is not None and spec.origin is not None
    return Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(spec.origin).resolve().parents[1])


def now_ms(offset_s: float = 0.0) -> int:
    """A whole-second timestamp near now (old timestamps are pruned by retention)."""
    return (int(time.time()) + int(offset_s)) * 1000


def windows(secret: str, n: int = 8) -> list[str]:
    """Every substring of *secret* of length *n* (FR-006 leak probe)."""
    return [secret[i:i + n] for i in range(len(secret) - n + 1)]


def leaks(text: str | bytes, secret: str) -> list[str]:
    hay = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    return [w for w in windows(secret) if w in hay]


def record(**kw: Any):
    """An ``LLMCallRecord`` with sensible defaults; ``key_fp`` passed only when given."""
    from nanobot.llm_usage.models import LLMCallRecord
    from nanobot.providers.base import LLMUsage

    tin = kw.pop("tin", 100)
    tout = kw.pop("tout", 10)
    base: dict[str, Any] = dict(
        started_at_ms=now_ms(-3600),
        duration_ms=50,
        provider="openrouter",
        model="openai/gpt-6-luna",
        source="api",
        stream=False,
        finish_reason="stop",
        usage=LLMUsage.reported(input_tokens=tin, output_tokens=tout),
    )
    base.update(kw)
    return LLMCallRecord(**base)


def keyed_provider(api_key: str | None, *, name: str = "openrouter", content: str = "ok",
                   tin: int = 100, tout: int = 10):
    """An OpenAI-compatible provider whose ``chat`` returns a scripted response (no network)."""
    from nanobot.providers.base import LLMResponse, LLMUsage
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    class _Offline(OpenAICompatProvider):
        _CHAT_RETRY_DELAYS = (0,)

        async def chat(self, **_kwargs: Any) -> LLMResponse:  # type: ignore[override]
            return LLMResponse(
                content=content,
                usage=LLMUsage.reported(input_tokens=tin, output_tokens=tout),
            )

        async def chat_stream(self, **_kwargs: Any) -> LLMResponse:  # type: ignore[override]
            return await self.chat()

    return _Offline(
        api_key=api_key,
        api_base="https://openrouter.ai/api/v1",
        default_model="openai/gpt-6-luna",
        provider_name=name,
    )


async def one_call(provider: Any, model: str = "openai/gpt-6-luna") -> Any:
    return await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hello"}], model=model,
    )


# -- pre-009 store shapes (FR-003 / SC-003) ---------------------------------------------------
# Faithful copies of nanobot/llm_usage/store.py on consolidate/new-main before this spec:
# the v1 CREATE TABLE plus the columns its additive migration had added by schema v4.

V1_LLM_CALLS_DDL = """
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
    error_kind TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx ON llm_calls(started_at_ms);
CREATE INDEX IF NOT EXISTS llm_calls_provider_model_time_idx
    ON llm_calls(provider, model, started_at_ms);
"""
V4_ADDED_COLUMNS = (
    ("tier", "TEXT"), ("cost_usd", "REAL"), ("call_id", "TEXT"), ("attempt", "INTEGER"),
    ("consumer", "TEXT"), ("agent", "TEXT"), ("session", "TEXT"), ("role", "TEXT"),
    ("purpose", "TEXT"), ("trace_id", "TEXT"), ("slot", "TEXT"), ("alias", "TEXT"),
    ("request_key", "TEXT"), ("prompt_version", "TEXT"), ("waste_label", "TEXT"),
    ("waste_set_by", "TEXT"), ("outcome", "TEXT"), ("price_source", "TEXT"),
    ("usage_source", "TEXT"), ("cost_billed", "INTEGER"), ("tokens_reasoning", "INTEGER"),
    ("tags", "TEXT"),
)
V4_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS llm_usage_events (
    id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL, call_id TEXT,
    attempt INTEGER, consumer TEXT, agent TEXT, session TEXT, role TEXT, purpose TEXT,
    trace_id TEXT, model TEXT, provider TEXT, alias TEXT, saved_tokens_in INTEGER,
    saved_tokens_out INTEGER, saved_cost_usd REAL, waste_label TEXT, reason_code TEXT,
    payload TEXT, dedupe_key TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def build_old_store(path: Path, *, version: int = 4, rows: int = 3) -> list[int]:
    """Create a pre-009 database at *path* (``version`` 1 or 4) holding *rows* calls.

    Returns the inserted ``started_at_ms`` values (distinct, near now).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(V1_LLM_CALLS_DDL)
        if version >= 4:
            for name, decl in V4_ADDED_COLUMNS:
                con.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} {decl}")
            con.executescript(V4_EVENTS_DDL)
        stamps = [now_ms(-7200 + 60 * i) for i in range(rows)]
        for i, ts in enumerate(stamps):
            if version >= 4:
                con.execute(
                    "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, "
                    "stream, finish_reason, input_tokens, output_tokens, total_tokens, "
                    "reported_tokens, estimated_tokens, tier, cost_usd, call_id, attempt, "
                    "consumer, outcome, price_source, usage_source, cost_billed) "
                    "VALUES (?, 10, 'openrouter', 'old-model', 'api', 0, 'stop', 100, 10, 110, "
                    "110, 0, 'fast', 0.01, ?, 1, 'moeka', 'ok', 'price_table', 'reported', 1)",
                    (ts, f"old-{i}"),
                )
            else:
                con.execute(
                    "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model, source, "
                    "stream, finish_reason, input_tokens, output_tokens, total_tokens, "
                    "reported_tokens, estimated_tokens) "
                    "VALUES (?, 10, 'openrouter', 'old-model', 'api', 0, 'stop', 100, 10, 110, "
                    "110, 0)",
                    (ts,),
                )
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
    finally:
        con.close()
    return stamps


def columns(path: Path) -> list[tuple[Any, ...]]:
    """``PRAGMA table_info(llm_calls)`` rows read straight from the file."""
    con = sqlite3.connect(path)
    try:
        return [tuple(r) for r in con.execute("PRAGMA table_info(llm_calls)")]
    finally:
        con.close()


def store_bytes(directory: Path) -> bytes:
    """Every byte of every file under *directory* (db, -wal, -shm, anything else)."""
    out = b""
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            out += p.read_bytes()
    return out
