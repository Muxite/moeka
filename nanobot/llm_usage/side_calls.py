"""Usage rows for model calls outside ``LLMProvider`` (spec 010 FR-003).

Image generation, transcription and ``VecStore`` embedding call a model without going
through an ``LLMProvider``, so the ledger observer never saw them (usage review 2026-10-01
#3). :func:`record_side_call` writes one ``llm_calls`` row per such call into the same
``llm_usage`` store:

- ``purpose`` is ``image``, ``transcription`` or ``embedding`` (also in ``tags.kind``);
- provider, model and ``key_fp`` (spec 009) of the paying key; ``source`` is the bound
  usage source (``current_llm_usage_source``);
- tokens when the backend reports them, else ``tags.units`` / ``tags.unit`` (images, audio
  bytes, texts) when known;
- a local embedder gets ``tier="local"`` and ``cost_usd=0.0``.

Recording is fail-open: any error is logged and swallowed, never raised into the call.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from loguru import logger

SideCallKind = Literal["image", "transcription", "embedding"]
SIDE_CALL_KINDS: tuple[str, ...] = ("image", "transcription", "embedding")

# Process-wide recorder override (a host or a test may point side calls elsewhere);
# ``None`` = ``record_llm_call`` into the store under ``data_dir`` (legacy data dir).
_RECORDER: Callable[[Any, Any], None] | None = None


def set_side_call_recorder(recorder: Callable[[Any, Any], None] | None) -> None:
    """Route side-call rows to ``recorder(call, event)``; ``None`` restores the default."""
    global _RECORDER
    _RECORDER = recorder


def now_ms() -> int:
    return int(time.time() * 1000)


def record_side_call(
    kind: SideCallKind,
    *,
    provider: str,
    model: str,
    started_at_ms: int,
    duration_ms: int,
    ok: bool = True,
    key: str | None = None,
    key_fp: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    units: int | None = None,
    unit: str | None = None,
    tier: str | None = None,
    cost_usd: float | None = None,
    cost_billed: bool | None = None,
    error_kind: str | None = None,
    data_dir: Path | None = None,
) -> bool:
    """Write one usage row for a side call; return False (and log) if that failed."""
    try:
        from nanobot.llm_usage.context import current_llm_usage_source
        from nanobot.llm_usage.models import LLMCallRecord
        from nanobot.providers.base import LLMUsage, key_fingerprint

        if kind not in SIDE_CALL_KINDS:
            raise ValueError(f"unknown side-call kind {kind!r}")
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = LLMUsage.reported(
                input_tokens=max(0, int(input_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
            )
        call = LLMCallRecord(
            started_at_ms=max(0, int(started_at_ms)),
            duration_ms=max(0, int(duration_ms)),
            provider=(provider or "unknown").strip() or "unknown",
            model=(model or "unknown").strip() or "unknown",
            source=current_llm_usage_source(),
            stream=False,
            finish_reason="stop" if ok else "error",
            usage=usage,
            error_kind=None if ok else (error_kind or "http"),
            tier=tier,
            cost_usd=cost_usd,
            key_fp=key_fp if key_fp is not None else key_fingerprint(key),
        )
        tags: dict[str, Any] = {"kind": kind}
        if units is not None:
            tags["units"] = int(units)
            if unit:
                tags["unit"] = unit
        event = SimpleNamespace(
            call_id=f"{kind}-{uuid.uuid4().hex}",
            attempt=1,
            purpose=kind,
            outcome="ok" if ok else "error",
            usage_source="reported" if usage is not None else "none",
            cost_is_billed=cost_billed,
            price_source=(
                "none" if cost_usd is None
                else "local_zero" if tier == "local" and cost_usd == 0.0
                else "provider_reported" if cost_billed else "price_table"
            ),
            tags=tags,
        )
        if _RECORDER is not None:
            _RECORDER(call, event)
        else:
            from nanobot.llm_usage import get_llm_usage_store

            get_llm_usage_store(data_dir=data_dir).record(call, event)
        return True
    except Exception:
        logger.exception("failed to record {} usage", kind)
        return False


__all__ = [
    "SIDE_CALL_KINDS",
    "SideCallKind",
    "now_ms",
    "record_side_call",
    "set_side_call_recorder",
]
