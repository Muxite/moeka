"""Programmatic API for nanobot/moeka.

- ``complete`` / ``acomplete``: one-shot programmatic completion for external
  services (e.g. awork) that want moeka's provider layer without the agent loop.
"""

from nanobot.api.complete import (
    acomplete,
    acomplete_json,
    acomplete_stream,
    complete,
    complete_json,
    complete_stream,
)

__all__ = [
    "acomplete",
    "acomplete_json",
    "acomplete_stream",
    "complete",
    "complete_json",
    "complete_stream",
]
