"""Trace sink contract for the kernel (stdlib + loguru only)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class TraceSink(Protocol):
    """Receives structured trace events. Implementations should not raise."""

    def emit(self, event: dict[str, Any]) -> None: ...


class NullTraceSink:
    """Discards every event."""

    def emit(self, event: dict[str, Any]) -> None:
        return None


class LoguruTraceSink:
    """Writes each event as a debug loguru record."""

    def emit(self, event: dict[str, Any]) -> None:
        logger.debug("trace {}", event)


def safe_emit(sink: TraceSink, event: dict[str, Any]) -> None:
    """Emit ``event`` to ``sink``; swallow and log any sink failure."""
    try:
        sink.emit(event)
    except Exception as exc:  # noqa: BLE001 - sinks must never break the caller
        try:
            logger.warning("trace sink {} failed: {!r}; event={}", type(sink).__name__, exc, event)
        except Exception:  # noqa: BLE001
            pass
