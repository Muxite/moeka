"""Request-local metadata for LLM usage records."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Literal

LLMUsageSource = Literal["user", "api", "cron", "dream", "system"]

_CURRENT_SOURCE: ContextVar[LLMUsageSource] = ContextVar(
    "nanobot_llm_usage_source",
    default="system",
)


def source_from_session_key(session_key: str | None) -> LLMUsageSource:
    """Classify a private session key without persisting that key."""
    key = session_key or ""
    if key.startswith("dream:"):
        return "dream"
    if key == "heartbeat" or key.startswith("cron:"):
        return "cron"
    if key.startswith("api:"):
        return "api"
    if key.startswith("system:"):
        return "system"
    return "user"


def source_from_request(
    session_key: str | None,
    *,
    channel: str | None,
    metadata: Mapping[str, object] | None,
) -> LLMUsageSource:
    """Classify a turn from trusted ingress metadata without retaining identifiers."""
    values = metadata or {}
    if isinstance(values.get("_cron_trigger"), Mapping):
        return "cron"
    if isinstance(values.get("_local_trigger"), Mapping):
        return "cron"
    if channel == "api":
        return "api"
    if channel == "system":
        return "system"
    return source_from_session_key(session_key)


def current_llm_usage_source() -> LLMUsageSource:
    return _CURRENT_SOURCE.get()


def bind_llm_usage_source(source: LLMUsageSource) -> Token[LLMUsageSource]:
    return _CURRENT_SOURCE.set(source)


def reset_llm_usage_source(token: Token[LLMUsageSource]) -> None:
    _CURRENT_SOURCE.reset(token)


@contextmanager
def llm_usage_source(source: LLMUsageSource) -> Generator[None]:
    """Bind a coarse usage source for nested provider calls."""
    token = bind_llm_usage_source(source)
    try:
        yield
    finally:
        reset_llm_usage_source(token)


# -- ledger attribution (Task 14): which schema slot / trace a call serves ----

_CURRENT_SLOT: ContextVar[str | None] = ContextVar("nanobot_llm_usage_slot", default=None)
_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar(
    "nanobot_llm_usage_trace_id",
    default=None,
)


def current_llm_usage_slot() -> str | None:
    return _CURRENT_SLOT.get()


def bind_llm_usage_slot(slot: str | None) -> Token[str | None]:
    return _CURRENT_SLOT.set(slot)


def reset_llm_usage_slot(token: Token[str | None]) -> None:
    _CURRENT_SLOT.reset(token)


@contextmanager
def llm_usage_slot(slot: str | None) -> Generator[None]:
    """Bind the schema slot nested provider calls are attributed to in the ledger."""
    token = bind_llm_usage_slot(slot)
    try:
        yield
    finally:
        reset_llm_usage_slot(token)


def current_llm_usage_trace_id() -> str | None:
    return _CURRENT_TRACE_ID.get()


def bind_llm_usage_trace_id(trace_id: str | None) -> Token[str | None]:
    return _CURRENT_TRACE_ID.set(trace_id)


def reset_llm_usage_trace_id(token: Token[str | None]) -> None:
    _CURRENT_TRACE_ID.reset(token)


@contextmanager
def llm_usage_trace_id(trace_id: str | None) -> Generator[None]:
    """Bind the trace id nested provider calls are attributed to in the ledger."""
    token = bind_llm_usage_trace_id(trace_id)
    try:
        yield
    finally:
        reset_llm_usage_trace_id(token)
