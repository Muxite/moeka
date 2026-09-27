"""Test helper: strip the stamp ``safe_emit`` adds when it is the empty default."""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {"trace_id": None, "span": None, "tags": {}}


def unstamped(event: dict[str, Any]) -> dict[str, Any]:
    """*event* without ``ts`` and without ``trace_id``/``span``/``tags`` at their
    outside-any-span defaults (explicit event values are kept)."""
    out = {k: v for k, v in event.items() if k != "ts"}
    for key, default in _DEFAULTS.items():
        if key in out and out[key] == default:
            del out[key]
    return out
