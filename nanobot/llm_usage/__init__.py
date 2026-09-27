"""Unified, content-free LLM usage backend."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_data_dir
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import LLMUsageStore

_STORES_LOCK = threading.Lock()
_STORES: dict[Path, LLMUsageStore] = {}


def empty_usage_payload() -> dict[str, Any]:
    return {
        "days": [],
        "total_tokens": 0,
        "total_tokens_30d": 0,
        "total_tokens_365d": 0,
        "reported_tokens_30d": 0,
        "estimated_tokens_30d": 0,
        "cache_read_tokens_30d": 0,
        "cache_read_observed_input_tokens_30d": 0,
        "cache_read_rate_30d": None,
        "peak_day_tokens": 0,
        "current_streak_days": 0,
        "longest_streak_days": 0,
        "active_days_30d": 0,
        "requests_30d": 0,
        "failed_requests_30d": 0,
        "providers_30d": [],
        "updated_at": None,
    }


def llm_usage_store_path(data_dir: Path | None = None) -> Path:
    """Usage DB under *data_dir* (``env.paths.data_dir``); ``None`` = legacy ``get_data_dir()``."""
    base = data_dir if data_dir is not None else get_data_dir()
    return base / "llm_usage.sqlite3"


def get_llm_usage_store(
    path: Path | None = None, *, data_dir: Path | None = None,
) -> LLMUsageStore:
    resolved = (path or llm_usage_store_path(data_dir)).resolve(strict=False)
    with _STORES_LOCK:
        store = _STORES.get(resolved)
        if store is None:
            store = LLMUsageStore(resolved)
            _STORES[resolved] = store
        return store


def record_llm_call(call: LLMCallRecord, *, data_dir: Path | None = None) -> None:
    """Default fail-open callback attached to gateway provider snapshots."""
    try:
        get_llm_usage_store(data_dir=data_dir).record(call)
    except Exception:
        logger.exception("failed to record LLM usage")


def llm_usage_recorder(data_dir: Path | None = None) -> Callable[[LLMCallRecord], None]:
    """A fail-open ``record_llm_call`` bound to a host data dir (``env.paths.data_dir``)."""

    def record(call: LLMCallRecord) -> None:
        record_llm_call(call, data_dir=data_dir)

    return record


def llm_usage_payload(
    *,
    days: int = 371,
    timezone_name: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    try:
        return get_llm_usage_store(data_dir=data_dir).usage_payload(
            days=days,
            timezone_name=timezone_name,
        )
    except Exception:
        logger.exception("failed to query LLM usage")
        return empty_usage_payload()


__all__ = [
    "LLMCallRecord",
    "LLMUsageStore",
    "empty_usage_payload",
    "get_llm_usage_store",
    "llm_usage_recorder",
    "record_llm_call",
    "llm_usage_store_path",
    "llm_usage_payload",
]
