"""Startup guard: host entry points must have the host-owned tools (spec 010 FR-004).

``AgentLoop`` defaults to ``host_tools=False`` (kernel hosts never get ``message``, ``cron``
or ``run_cli_app``). A host entry point that forgets ``host_tools=True`` would start with
those tools silently missing (architecture review 2026-10-01, R4). The gateway, ``serve``
and the CLI agent call :func:`require_host_tools` right after building the loop; a
missing tool fails startup with exit 2 and names the tools.

A tool the config deliberately turns off is not expected: ``run_cli_app`` when
``tools.cliApps.enable`` is false, and any tool in ``agents.defaults.toolsDeny`` or left out
of a set ``agents.defaults.toolsAllow``.
"""

from __future__ import annotations

from typing import Any

import typer

HOST_TOOLS: tuple[str, ...] = ("message", "cron", "run_cli_app")
EXIT_MISSING_HOST_TOOLS = 2


def expected_host_tools(config: Any) -> list[str]:
    """The host tools *config* expects a host loop to register."""
    defaults = config.agents.defaults
    deny = set(defaults.tools_deny or ())
    allow = set(defaults.tools_allow) if defaults.tools_allow is not None else None
    expected = []
    for name in HOST_TOOLS:
        if name in deny or (allow is not None and name not in allow):
            continue
        if name == "run_cli_app" and not config.tools.cli_apps.enable:
            continue
        expected.append(name)
    return expected


def missing_host_tools(loop: Any, config: Any) -> list[str]:
    """Expected host tools that *loop* did not register."""
    registered = set(loop.tools.tool_names)
    return [name for name in expected_host_tools(config) if name not in registered]


def require_host_tools(loop: Any, config: Any, *, entry: str) -> None:
    """Exit 2 with a message naming the missing host tools, if any."""
    missing = missing_host_tools(loop, config)
    if not missing:
        return
    typer.echo(
        f"Error: {entry} started without host tool(s) {', '.join(missing)}; the agent loop "
        "must be built with host_tools=True (and a cron service for 'cron').",
        err=True,
    )
    raise typer.Exit(EXIT_MISSING_HOST_TOOLS)


__all__ = [
    "EXIT_MISSING_HOST_TOOLS",
    "HOST_TOOLS",
    "expected_host_tools",
    "missing_host_tools",
    "require_host_tools",
]
