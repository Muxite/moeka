"""Shared handling for session-bound automation turns."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

AUTOMATION_HISTORY_META = "_automation_turn"
# Markers written by retired automation sources (the removed cron scheduler
# stamped ``_cron_turn`` and kind ``"cron"``; the removed local-trigger
# subsystem stamped kind ``"local_trigger"``). Session history persisted before
# their removal must stay hidden and labelled, so these are still honoured when
# reading history; nothing produces them any more.
_RETIRED_HISTORY_META_KEYS = ("_cron_turn",)
_RETIRED_AUTOMATION_KINDS = ("cron", "local_trigger")


@dataclass(frozen=True)
class AutomationTurnSpec:
    """Source-specific wiring for one session-bound automation turn type."""

    kind: str
    trigger_meta_key: str
    legacy_history_meta_key: str | None = None
    history_fields: Mapping[str, str] = field(default_factory=dict[str, str])
    text_builder: Callable[[Mapping[str, Any]], str | None] | None = None


def automation_trigger(
    metadata: Mapping[str, Any] | None,
    spec: AutomationTurnSpec,
) -> dict[str, Any] | None:
    """Return source trigger metadata for *spec* when present."""
    raw = (metadata or {}).get(spec.trigger_meta_key)
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else None


def automation_history_overrides_for_spec(
    metadata: Mapping[str, Any] | None,
    spec: AutomationTurnSpec,
) -> tuple[str | None, dict[str, Any]]:
    """Return hidden session-history text/metadata overrides for *spec*."""
    trigger = automation_trigger(metadata, spec)
    if not trigger:
        return None, {}

    details: dict[str, Any] = {"kind": spec.kind}
    extra: dict[str, Any] = {AUTOMATION_HISTORY_META: details}
    if spec.legacy_history_meta_key:
        extra[spec.legacy_history_meta_key] = True
    for history_key, trigger_key in spec.history_fields.items():
        value = trigger.get(trigger_key)
        extra[history_key] = value
        details[history_key] = value

    text = spec.text_builder(trigger) if spec.text_builder else None
    return text, extra


@lru_cache(maxsize=1)
def _automation_specs() -> tuple[AutomationTurnSpec, ...]:
    # No live automation sources remain; retired ones are read-only markers above.
    return ()


def automation_history_overrides(
    metadata: Mapping[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Return session-history text/metadata overrides for supported automation turns."""
    for spec in _automation_specs():
        text, extra = automation_history_overrides_for_spec(metadata, spec)
        if extra:
            return text, extra
    return None, {}


def is_automation_history_message(message: Mapping[str, Any] | None) -> bool:
    """True for hidden automation trigger records in session history."""
    if not message:
        return False
    marker = message.get(AUTOMATION_HISTORY_META)
    if marker is True or isinstance(marker, Mapping):
        return True
    if any(message.get(key) is True for key in _RETIRED_HISTORY_META_KEYS):
        return True
    return any(
        spec.legacy_history_meta_key
        and message.get(spec.legacy_history_meta_key) is True
        for spec in _automation_specs()
    )


def is_automation_kind(value: Any) -> bool:
    return isinstance(value, str) and (
        value == "trigger"
        or value in _RETIRED_AUTOMATION_KINDS
        or any(spec.kind == value for spec in _automation_specs())
    )
