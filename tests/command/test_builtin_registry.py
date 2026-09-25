"""The builtin router registers exactly the expected slash commands."""

from __future__ import annotations

from nanobot.command.builtin import register_builtin_commands
from nanobot.command.router import CommandRouter

EXACT = [
    "/__shell", "/compact", "/dream", "/dream-log", "/dream-prompt", "/dream-restore",
    "/goal", "/help", "/history", "/model", "/new", "/skill", "/status",
]
PREFIX = [
    "/__shell ", "/dream-log ", "/dream-prompt ", "/dream-restore ",
    "/goal ", "/history ", "/model ",
]


def test_registered_command_names() -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    # Introspection: the router's private _exact / _prefix registries
    # (no public listing API).
    assert sorted(router._exact) == EXACT
    assert sorted(p for p, _ in router._prefix) == PREFIX
