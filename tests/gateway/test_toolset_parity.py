"""Toolset parity: gateway loops get the host tools, kernel agents never do.

``AgentLoop`` defaults to ``host_tools=False``. A gateway path that forgets to pass
``host_tools=True`` silently drops ``message``, ``cron`` and ``run_cli_app`` from the
live bot, with no error (architecture review 2026-10-01, risk R4; spec 003 T024).
This file guards both directions:

* behaviour: a host loop registers the host tools; a bare loop and a ``moeka.Kernel``
  agent do not;
* source: every ``AgentLoop`` construction in a gateway/host module passes the literal
  ``host_tools=True``, and no kernel module passes it at all.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService

HOST_TOOLS = {"message", "cron", "run_cli_app"}

REPO = Path(__file__).resolve().parents[2]
# Modules that run a gateway/host loop: the gateway runtime, `nanobot agent`,
# `nanobot serve` (HTTP API) and the gateway service package.
HOST_DIRS = ("nanobot/cli", "nanobot/gateway", "nanobot/api")
# Modules that must never opt into host tools (the kernel and its public package).
KERNEL_DIRS = ("nanobot/kernel", "moeka")
# The construction sites known today; the scan below must keep finding them, so a
# refactor that hides a construction from the scan fails loudly instead of passing.
KNOWN_HOST_SITES = {
    "nanobot/cli/gateway_runtime.py",
    "nanobot/cli/commands.py",
    "nanobot/cli/agent.py",
}


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def _loop(tmp_path: Path, *, host_tools: bool) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
        host_tools=host_tools,
    )


def test_host_loop_registers_message_cron_and_cli_app_tools(tmp_path: Path) -> None:
    names = set(_loop(tmp_path, host_tools=True).tools.tool_names)
    assert HOST_TOOLS <= names, f"missing host tools: {sorted(HOST_TOOLS - names)}"


def test_loop_without_host_tools_has_none_of_them(tmp_path: Path) -> None:
    names = set(_loop(tmp_path, host_tools=False).tools.tool_names)
    assert not (HOST_TOOLS & names), f"host tools leaked: {sorted(HOST_TOOLS & names)}"


def test_host_and_bare_loop_differ_by_exactly_the_host_tools(tmp_path: Path) -> None:
    host = set(_loop(tmp_path / "h", host_tools=True).tools.tool_names)
    bare = set(_loop(tmp_path / "b", host_tools=False).tools.tool_names)
    assert host - bare == HOST_TOOLS
    assert bare <= host


def test_kernel_agent_never_registers_host_tools(tmp_path: Path) -> None:
    from moeka.agents import AgentSpec
    from moeka.testing import FakeProvider
    from moeka.trace import MemoryTraceSink
    from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
    from nanobot.kernel.kernel import Kernel

    env = Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
        trace=MemoryTraceSink(),
    )
    with Kernel(env) as kernel:
        kernel.llm.register_provider(
            "main", FakeProvider(["ok"]),
            ModelSpec(name="main", model="fake-main", provider="openai", max_tokens=100),
        )
        names = {t.name for t in kernel.agent(AgentSpec(name="parity")).tools}
    assert names, "kernel agent exposed no tools at all; the probe is broken"
    assert not (HOST_TOOLS & names), f"kernel agent got host tools: {sorted(HOST_TOOLS & names)}"


# -- source scan ------------------------------------------------------------------------


def _is_loop_construction(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "AgentLoop"
    if isinstance(func, ast.Attribute):
        if func.attr == "AgentLoop":
            return True
        if func.attr == "from_config" and isinstance(func.value, ast.Name):
            name = func.value.id
            # `AgentLoop.from_config(...)` or an injected class such as `agent_loop_class`.
            return name == "AgentLoop" or "loop" in name.lower()
    return False


def _host_tools_kw(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "host_tools":
            return kw.value
    return None


def _constructions(rel_dirs: tuple[str, ...]):
    for rel in rel_dirs:
        for path in sorted((REPO / rel).rglob("*.py")):
            if "tests" in path.relative_to(REPO).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_loop_construction(node):
                    yield path.relative_to(REPO).as_posix(), node


def test_every_gateway_loop_construction_passes_host_tools_true() -> None:
    sites = list(_constructions(HOST_DIRS))
    bad = []
    for rel, call in sites:
        value = _host_tools_kw(call)
        if not (isinstance(value, ast.Constant) and value.value is True):
            bad.append(f"{rel}:{call.lineno}")
    assert not bad, (
        "gateway AgentLoop built without host_tools=True (drops message/cron/run_cli_app): "
        + ", ".join(bad)
    )
    found = {rel for rel, _ in sites}
    assert KNOWN_HOST_SITES <= found, f"scan lost known sites: {sorted(KNOWN_HOST_SITES - found)}"


def test_no_kernel_module_opts_into_host_tools() -> None:
    sites = list(_constructions(KERNEL_DIRS))
    assert sites, "no AgentLoop construction found in the kernel; the scan is broken"
    bad = [f"{rel}:{call.lineno}" for rel, call in sites if _host_tools_kw(call) is not None]
    assert not bad, f"kernel AgentLoop must not pass host_tools: {', '.join(bad)}"


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("AgentLoop.from_config(cfg, bus)", True),
        ("AgentLoop.from_config(cfg, bus, host_tools=False)", True),
        ("agent_loop_class.from_config(cfg, host_tools=flag)", True),
        ("AgentLoop(bus=b, host_tools=True)", False),
        ("x.AgentLoop.from_config(cfg)", False),  # not a loop name: outside the scan
    ],
)
def test_scan_rule_on_examples(source: str, flagged: bool) -> None:
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    if not _is_loop_construction(call):
        assert not flagged
        return
    value = _host_tools_kw(call)
    is_bad = not (isinstance(value, ast.Constant) and value.value is True)
    assert is_bad is flagged
