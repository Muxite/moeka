"""The builtin router registers exactly the expected slash commands.

/evaluator-prompt, /pairing and /trigger are gateway-owned commands carried over from main
(003 consolidation); they still register for kernel loops (open item: gate behind host_tools).
"""

from __future__ import annotations

from nanobot.command.builtin import register_builtin_commands
from nanobot.command.router import CommandRouter

EXACT = [
    "/__shell", "/compact", "/dream", "/dream-log", "/dream-prompt", "/dream-restore",
    "/evaluator-prompt", "/goal", "/help", "/history", "/model", "/new", "/pairing",
    "/skill", "/status", "/trigger",
]
PREFIX = [
    "/__shell ", "/dream-log ", "/dream-prompt ", "/dream-restore ",
    "/evaluator-prompt ", "/goal ", "/history ", "/model ", "/pairing ", "/trigger ",
]


def test_registered_command_names() -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    # Introspection: the router's private _exact / _prefix registries
    # (no public listing API).
    assert sorted(router._exact) == EXACT
    assert sorted(p for p, _ in router._prefix) == PREFIX
