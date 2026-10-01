"""Agents on a kernel (Task 9B): actions, MCP, plugins, offline and streaming."""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import threading
import time
import uuid
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from moeka.agents import AgentSpec, AgentStream, RunLimits, RunResult, StreamEvent
from moeka.errors import BudgetExceeded
from moeka.testing import FakeProvider
from moeka.tools import (
    CapabilityRequest,
    DefaultPolicy,
    FunctionTool,
    MCPServer,
    OfflinePolicy,
    PluginRegistry,
)
from moeka.trace import MemoryTraceSink
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.manifest import MANIFEST_FILENAME, PluginManifest, compute_version_hash
from nanobot.kernel.policy import (
    OFFLINE_MARKER,
    POLICY_MARKER,
    Allow,
    Deny,
    IntersectionPolicy,
    Principal,
)
from nanobot.kernel.typed import RESULT_SCHEMA_MARKER
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)
AGENT = Principal("agent", "agent")


def _env(tmp_path: Path, sink: Any, *, offline: bool = False,
         exec_base_env: dict[str, str] | None = None, state: str = "state") -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / state,
        # Spec 005: one writer per state dir; extra kernels in one test get their
        # own state dir and share the first one's data dir (usage store).
        data_dir=None if state == "state" else tmp_path / "state" / "data",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
        trace=sink,
        offline=offline,
        exec_base_env=exec_base_env,
    )


def tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=10, output_tokens=5),
    )


def _tool_message(call: Any, name: str) -> str:
    """The content of the tool result named *name* the model was sent in *call*."""
    for message in call.messages:
        if message.get("role") == "tool" and message.get("name") == name:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    raise AssertionError(f"no {name} tool result in {call.messages!r}")


@pytest.fixture
def sink() -> MemoryTraceSink:
    return MemoryTraceSink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(fake: FakeProvider | None = None, *, offline: bool = False,
             exec_base_env: dict[str, str] | None = None, **kwargs: Any) -> Kernel:
        kernel = Kernel(
            _env(tmp_path, sink, offline=offline, exec_base_env=exec_base_env,
                 state="state" if not kernels else f"state-{len(kernels) + 1}"), **kwargs,
        )
        if fake is not None:
            kernel.llm.register_provider("main", fake, MAIN)
        kernels.append(kernel)
        return kernel

    yield make
    for kernel in kernels:
        kernel.close()


# -- actions ----------------------------------------------------------------------------


class Weather(BaseModel):
    city: str
    celsius: float


async def test_typed_read_only_action_reaches_the_model(make_kernel) -> None:
    def weather(city: str) -> dict[str, Any]:
        """Current weather for a city."""
        return {"city": city, "celsius": 21.5}

    fake = FakeProvider([tool_call("weather", {"city": "Oslo"}), "It is mild."])
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="w"))
    assert agent.add_action(weather, read_only=True, output_model=Weather) == "weather"
    info = next(t for t in agent.tools if t.name == "weather")
    assert info.read_only is True and info.description == "Current weather for a city."
    result = await agent.run("weather in Oslo?")
    assert result.stop_reason == "completed" and result.content == "It is mild."
    assert "weather" in result.tools_used
    sent = json.loads(_tool_message(fake.calls[1], "weather"))
    assert sent == {"city": "Oslo", "celsius": 21.5}


async def test_schema_violating_action_result_is_a_tool_error(make_kernel) -> None:
    fake = FakeProvider([tool_call("weather", {"city": "Oslo"}), "sorry"])
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(
        name="w",
        actions=[FunctionTool(lambda city: {"city": city}, name="weather",
                              output_model=Weather)],
    ))
    result = await agent.run("weather?")
    assert result.stop_reason == "completed"
    assert RESULT_SCHEMA_MARKER in _tool_message(fake.calls[1], "weather")


async def test_sync_action_runs_off_the_loop_and_can_call_the_kernel(make_kernel, sink) -> None:
    # A sync action runs in a worker thread: blocking *_sync kernel calls work from it
    # (on the loop thread they would deadlock), and the run's span still applies.
    fake = FakeProvider([tool_call("summarize", {"text": "moeka"}), "inner", "done"])
    kernel = make_kernel(fake)
    threads: list[threading.Thread] = []

    def summarize(text: str) -> str:
        threads.append(threading.current_thread())
        return kernel.llm.complete_sync(f"summarize {text}").text

    agent = kernel.agent(AgentSpec(name="s", actions=[summarize]))
    result = await agent.run("go")
    assert result.stop_reason == "completed" and result.content == "done"
    assert _tool_message(fake.calls[2], "summarize") == "inner"
    assert threads and threads[0] is not kernel._bridge._thread
    calls = [e for e in sink.events if e.get("event") == "model.call"]
    assert len(calls) == 3 and {e.get("trace_id") for e in calls} == {result.trace_id}


async def test_blocking_sync_action_does_not_stall_other_runs(make_kernel) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow() -> str:
        started.set()
        release.wait(10)
        return "slow done"

    kernel = make_kernel(FakeProvider([tool_call("slow", {}), "a done"]))
    kernel.llm.register_provider(
        "other", FakeProvider(["b done"]),
        ModelSpec(name="other", model="fake-other", provider="openai"),
    )
    blocked = kernel.agent(AgentSpec(name="a", actions=[slow]))
    free = kernel.agent(AgentSpec(name="b", model="other"))
    task = asyncio.create_task(blocked.run("block"))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        other = await asyncio.wait_for(free.run("hi"), 10)
        assert other.content == "b done"
        assert not task.done()
    finally:
        release.set()
    assert (await task).content == "a done"


async def test_sync_actions_run_on_the_kernels_own_action_pool(make_kernel) -> None:
    # Blocking actions get their own pool, not the loop's default executor (which
    # memory search and built-in tools use), so they cannot starve the kernel.
    with pytest.raises(ValueError, match="action_workers"):
        make_kernel(FakeProvider(), action_workers=0)
    names: list[str] = []

    def which() -> str:
        names.append(threading.current_thread().name)
        return "ok"

    shared = FunctionTool(which, name="shared")  # a host-built tool gets the pool too
    kernel = make_kernel(FakeProvider([tool_call("which", {}), tool_call("shared", {}),
                                       "done"]), action_workers=2)
    agent = kernel.agent(AgentSpec(name="p", actions=[which, shared]))
    assert (await agent.run("go")).content == "done"
    assert len(names) == 2 and all(n.startswith("moeka-action") for n in names)
    pool = kernel._action_pool
    assert pool is not None and pool._max_workers == 2
    await asyncio.to_thread(kernel.close)
    assert pool._shutdown
    # Outside a kernel run, a FunctionTool still uses asyncio.to_thread.
    names.clear()
    assert await shared.execute() == "ok"
    assert names and not names[0].startswith("moeka-action")


def test_spec_actions_are_registered_and_checked(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())

    def ping() -> str:
        return "pong"

    agent = kernel.agent(AgentSpec(name="a", actions=[ping]))
    assert "ping" in {t.name for t in agent.tools}
    with pytest.raises(ValueError, match="tools_deny"):
        kernel.agent(AgentSpec(name="d", actions=[ping], tools_deny=["ping"]))
    with pytest.raises(ValueError, match="tools_allow"):
        kernel.agent(AgentSpec(name="l", actions=[ping], tools_allow=["read_file"]))
    with pytest.raises(ValueError, match="two actions"):
        kernel.agent(AgentSpec(name="t", actions=[ping, FunctionTool(ping)]))
    with pytest.raises(TypeError):
        AgentSpec(name="x", actions=[42])
    # A spec action named like a built-in fails when the loop is built.
    clash = kernel.agent(AgentSpec(name="c", actions=[FunctionTool(ping, name="read_file")]))
    with pytest.raises(ValueError, match="already has a tool"):
        clash.tools  # noqa: B018


def test_add_action_denied_raises(make_kernel) -> None:
    policy = DefaultPolicy(deny_capabilities=frozenset({"net.fetch"}))
    kernel = make_kernel(FakeProvider(), policy=policy)
    agent = kernel.agent(AgentSpec(name="a", tools_deny=["secret"]))

    def fetch(url: str) -> str:
        return url

    with pytest.raises(ValueError, match="tools_deny"):
        agent.add_action(fetch, name="secret")
    with pytest.raises(ValueError, match=r"denies \['net.fetch'\]"):
        agent.add_action(fetch, capabilities=["fs.read", "net.fetch"])
    assert agent.add_action(fetch, capabilities=["fs.read"]) == "fetch"
    scoped = kernel.agent(AgentSpec(name="s", tools_allow=["read_file"]))
    with pytest.raises(ValueError, match="tools_allow"):
        scoped.add_action(fetch)
    # Nothing was registered by the refused calls.
    assert {t.name for t in agent.tools} >= {"fetch"}
    assert "secret" not in {t.name for t in agent.tools}


def test_add_action_name_collision_and_replace(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    agent = kernel.agent(AgentSpec(name="a"))

    def read_file(path: str) -> str:
        return "mine"

    with pytest.raises(ValueError, match="replace=True"):
        agent.add_action(read_file)
    assert agent.add_action(read_file, replace=True) == "read_file"
    assert isinstance(agent._loop.tools.get("read_file"), FunctionTool)
    with pytest.raises(TypeError):
        agent.add_action(agent._loop.tools.get("list_dir"), read_only=True)


def test_add_action_from_another_thread(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    agent = kernel.agent(AgentSpec(name="a"))
    names: list[str] = []
    thread = threading.Thread(target=lambda: names.append(agent.add_action(lambda: "x",
                                                                            name="t1")))
    thread.start()
    thread.join()
    assert names == ["t1"] and "t1" in {t.name for t in agent.tools}


async def test_action_capabilities_are_gated_per_call(make_kernel) -> None:
    policy = DefaultPolicy(deny_rules=(("fs.read", "/secret/*"),))
    fake = FakeProvider([tool_call("peek", {}), "done"])
    kernel = make_kernel(fake, policy=policy)
    agent = kernel.agent(AgentSpec(name="g"))
    ran: list[bool] = []
    agent.add_action(lambda: ran.append(True) or "seen", name="peek",
                     capabilities=[CapabilityRequest("fs.read", "/secret/key")])
    await agent.run("peek")
    assert ran == []
    assert POLICY_MARKER in _tool_message(fake.calls[1], "peek")


def test_function_tool_capabilities() -> None:
    tool = FunctionTool(lambda: None, name="t",
                        capabilities=["net.fetch", CapabilityRequest("fs.read", "/a")])
    assert tool.capability_surface() == frozenset({"net.fetch", "fs.read"})
    assert tool.capabilities({}) == [CapabilityRequest("net.fetch", ""),
                                     CapabilityRequest("fs.read", "/a")]
    assert FunctionTool(lambda: None, name="u").capability_surface() == frozenset()
    with pytest.raises(TypeError):
        FunctionTool(lambda: None, capabilities="net.fetch")  # type: ignore[arg-type]


def test_registry_admit_rejects_without_removing() -> None:
    registry = ToolRegistry()
    first = FunctionTool(lambda: None, name="t")
    assert registry.register(first)
    registry.admit = lambda tool: tool.name != "t"
    assert registry.register(FunctionTool(lambda: None, name="t")) is False
    assert registry.get("t") is first


# -- MCP ----------------------------------------------------------------------------------


_MCP_SERVER = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stub")


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back."""
    return "echo:" + text


@mcp.tool()
def hidden() -> str:
    """Denied by the spec."""
    return "hidden"


mcp.run()
'''


async def test_mcp_servers_listed_called_and_closed(make_kernel, sink, tmp_path) -> None:
    script = tmp_path / "stub_mcp.py"
    script.write_text(_MCP_SERVER, encoding="utf-8")
    fake = FakeProvider([tool_call("mcp_stub_echo", {"text": "hi"}), "echoed"])
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(
        name="m",
        mcp_servers={
            "stub": {"command": sys.executable, "args": [str(script)]},
            "broken": MCPServer(command=str(tmp_path / "no-such-mcp-binary")),
        },
        tools_deny=["mcp_stub_hidden"],
    ))
    names = {t.name for t in agent.tools}
    assert "mcp_stub_echo" in names and "mcp_stub_hidden" not in names
    errors = [e for e in sink.events if e["event"] == "mcp.error"]
    assert [(e["agent"], e["server"]) for e in errors] == [("m", "broken")]

    result = await agent.run("echo hi")
    assert result.stop_reason == "completed" and "mcp_stub_echo" in result.tools_used
    assert "echo:hi" in _tool_message(fake.calls[1], "mcp_stub_echo")

    provider = agent._mcp
    assert provider is not None and provider.connected_server_names == {"stub"}
    await agent.aclose()
    assert provider.connected_server_names == set()


_ENV_MCP_SERVER = '''
import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("envdump")


@mcp.tool()
def environ() -> str:
    """The server process environment."""
    return json.dumps(dict(os.environ))


mcp.run()
'''


async def test_stdio_mcp_server_sees_only_the_host_env(
    make_kernel, tmp_path, monkeypatch,
) -> None:
    script = tmp_path / "env_mcp.py"
    script.write_text(_ENV_MCP_SERVER, encoding="utf-8")
    monkeypatch.setenv("HOME", "/poisoned-home")
    monkeypatch.setenv("USER", "poisoned-user")
    fake = FakeProvider([tool_call("mcp_envdump_environ", {}), "ok"])
    kernel = make_kernel(fake, exec_base_env={"PATH": "/x"})
    agent = kernel.agent(AgentSpec(name="e", mcp_servers={
        "envdump": {"command": sys.executable, "args": [str(script)],
                    "env": {"SERVER_OWN": "1"}},
    }))
    result = await agent.run("dump env")
    text = _tool_message(fake.calls[1], "mcp_envdump_environ")
    child = json.loads(text[text.index("{"):])  # after the untrusted-content banner
    assert child.get("PATH") == "/x" and child.get("SERVER_OWN") == "1"
    assert "/poisoned-home" not in child.values() and "poisoned-user" not in child.values()
    assert "HOME" not in child and "USER" not in child


def test_mcp_spec_values_are_validated() -> None:
    spec = AgentSpec(name="x", mcp_servers={"s": {"command": "srv", "args": ["-v"]}})
    same = AgentSpec(name="x", mcp_servers={"s": MCPServer(command="srv", args=["-v"])})
    assert spec == same and hash(spec) == hash(same)
    with pytest.raises(ValueError):
        AgentSpec(name="x", mcp_servers={"s": {"type": "carrier-pigeon"}})


# -- plugins --------------------------------------------------------------------------------


_PLUGIN_SRC = '''
from typing import Any

from nanobot.agent.tools.base import Tool


class PluginTool(Tool):
    _scopes = {{"core", "subagent"}}

    @property
    def name(self) -> str:
        return {tool_name!r}

    @property
    def description(self) -> str:
        return "kernel agent test plugin"

    @property
    def parameters(self) -> dict[str, Any]:
        return {{"type": "object", "properties": {{}}}}

    async def execute(self, **kwargs: Any) -> Any:
        return "plugin ran"
'''

HOST = Principal("host", "host")


class _Plugin:
    def __init__(self, base: Path, name: str) -> None:
        self.pkg = f"kagent_{uuid.uuid4().hex[:10]}"
        self.name = name
        root = base / self.pkg
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        (root / "tool.py").write_text(_PLUGIN_SRC.format(tool_name=name), encoding="utf-8")
        self.root = root
        self.value = f"{self.pkg}.tool:PluginTool"
        raw: dict[str, Any] = {
            "name": name, "kind": "tool", "version": "1.0.0", "version_hash": "0" * 64,
            "tier": 2, "entry": self.value, "capabilities_requested": [],
        }
        raw["version_hash"] = compute_version_hash(root, raw)
        (root / MANIFEST_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
        self.manifest = PluginManifest.model_validate(raw)

    def entry_point(self) -> EntryPoint:
        return EntryPoint(name=self.name, value=self.value, group="nanobot.tools")


@pytest.fixture
def plugin_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "site"
    base.mkdir()
    monkeypatch.syspath_prepend(str(base))
    before = set(sys.modules)
    yield base
    for mod in set(sys.modules) - before:
        if mod.startswith("kagent_"):
            sys.modules.pop(mod, None)


def _agent_and_subagent_tools(agent: Any) -> tuple[set[str], set[str]]:
    names = {t.name for t in agent.tools}
    return names, set(agent._loop.subagents._build_tools().tool_names)


def test_kernel_plugins_gate_agent_and_subagent_tools(make_kernel, plugin_base, tmp_path):
    active, inactive = _Plugin(plugin_base, "kp_active"), _Plugin(plugin_base, "kp_idle")
    registry = PluginRegistry(tmp_path / "plugins")
    for plugin in (active, inactive):
        registry.register(plugin.manifest, plugin.root, principal=HOST)
    registry.activate(active.name, active.manifest.version_hash, HOST)
    eps = [active.entry_point(), inactive.entry_point()]
    with patch("nanobot.agent.tools.loader.entry_points", return_value=eps):
        kernel = make_kernel(FakeProvider(), plugins=registry)
        assert kernel.plugins is registry
        agent_tools, sub_tools = _agent_and_subagent_tools(kernel.agent(AgentSpec(name="p")))
        assert "kp_active" in agent_tools and "kp_active" in sub_tools
        assert "kp_idle" not in agent_tools and "kp_idle" not in sub_tools
        # No registry: the legacy loader registers every entry-point plugin.
        legacy = make_kernel(FakeProvider())
        agent_tools, sub_tools = _agent_and_subagent_tools(legacy.agent(AgentSpec(name="l")))
        assert {"kp_active", "kp_idle"} <= agent_tools
        assert {"kp_active", "kp_idle"} <= sub_tools
    with pytest.raises(TypeError):
        Kernel(_env(tmp_path, None), plugins="registry")  # type: ignore[arg-type]


# -- offline --------------------------------------------------------------------------------


class _NetTool(Tool):
    _capability_names = frozenset({"fs.read", "net.fetch"})

    @property
    def name(self) -> str:
        return "net_tool"

    @property
    def description(self) -> str:
        return "declares net.fetch"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest("net.fetch", "https://example.com")]

    async def execute(self, **kwargs: Any) -> Any:
        return "fetched"


def _assert_offline(agent: Any) -> None:
    names = {t.name for t in agent.tools}
    assert "web_search" not in names and "web_fetch" not in names
    assert {"read_file", "exec"} <= names  # exec stays: the sandbox is the boundary
    loop = agent._loop
    for name in names:
        surface = loop.tools.get(name).capability_surface() or frozenset()
        assert not {c for c in surface if c.startswith("net.") or c == "mcp.call"}, name
    decision = loop.tools.gate_policy.decide(
        AGENT, CapabilityRequest("net.fetch", "https://example.com"), None,
    )
    assert isinstance(decision, Deny) and decision.marker == OFFLINE_MARKER
    assert OFFLINE_MARKER in decision.reason
    with pytest.raises(ValueError, match="offline"):
        agent.add_action(lambda: "x", name="fetcher", capabilities=["net.fetch"])
    with pytest.raises(ValueError, match="offline"):
        agent.add_action(_NetTool())
    loop.tools.register(_NetTool())  # the registry refuses it too, whoever registers
    assert not loop.tools.has("net_tool")


def test_offline_spec(make_kernel) -> None:
    kernel = make_kernel(FakeProvider(), policy=DefaultPolicy())
    online = {t.name for t in kernel.agent(AgentSpec(name="on")).tools}
    assert {"web_search", "web_fetch"} <= online
    agent = kernel.agent(AgentSpec(name="off", offline=True))
    _assert_offline(agent)
    policy = agent._loop.policy
    assert isinstance(policy, IntersectionPolicy)
    assert any(isinstance(m, OfflinePolicy) for m in policy.members)


def test_offline_env(make_kernel) -> None:
    kernel = make_kernel(FakeProvider(), offline=True)
    _assert_offline(kernel.agent(AgentSpec(name="off")))
    with pytest.raises(ValueError, match="env.offline"):
        kernel.agent(AgentSpec(name="m", mcp_servers={"s": {"command": "srv"}}))


def test_offline_with_mcp_servers_raises(make_kernel) -> None:
    kernel = make_kernel(FakeProvider())
    with pytest.raises(ValueError, match="mcp_servers"):
        kernel.agent(AgentSpec(name="m", offline=True, mcp_servers={"s": {"command": "x"}}))


def test_offline_policy() -> None:
    policy = OfflinePolicy()
    assert policy.denies_everywhere("net.fetch") and policy.denies_everywhere("mcp.call")
    assert policy.denies_everywhere("net.anything")
    assert not policy.denies_everywhere("fs.read")
    assert isinstance(policy.decide(AGENT, CapabilityRequest("fs.read", "/x"), None), Allow)
    denied = policy.decide(AGENT, CapabilityRequest("mcp.call", "srv"), None)
    assert isinstance(denied, Deny) and denied.marker == OFFLINE_MARKER
    child = policy.attenuate(frozenset({"fs.read", "net.fetch"}))
    assert isinstance(child.decide(AGENT, CapabilityRequest("net.fetch", "u"), None), Deny)
    assert isinstance(child.decide(AGENT, CapabilityRequest("fs.write", "/x"), None), Deny)
    assert isinstance(child.decide(AGENT, CapabilityRequest("fs.read", "/x"), None), Allow)
    assert OfflinePolicy() == policy and hash(OfflinePolicy()) == hash(policy)


# -- streaming ---------------------------------------------------------------------------


LIST_DIR = tool_call("list_dir", {"path": "."})


async def test_stream_event_sequence_and_result(make_kernel, sink) -> None:
    fake = FakeProvider([LIST_DIR, "final answer", LIST_DIR, "final answer"])
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="s"))
    stream = agent.stream("list it", session="one")
    assert isinstance(stream, AgentStream)
    events: list[StreamEvent] = [event async for event in stream]
    types = [e.type for e in events]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert types.index("tool.started") < types.index("tool.completed") \
        < types.index("text.delta") < types.index("text.completed")
    assert "".join(e.delta for e in events if e.type == "text.delta") == "final answer"
    started = next(e for e in events if e.type == "tool.started")
    assert started.name == "list_dir" and started.arguments == {"path": "."}
    result = await stream.result()
    assert isinstance(result, RunResult)
    assert events[-1].result is result
    assert events[-1].metadata["stop_reason"] == "completed"
    # The same shape run() returns for the same script.
    ran = await agent.run("list it", session="two")
    for field in ("content", "stop_reason", "iterations", "tools_used", "error", "question"):
        assert getattr(result, field) == getattr(ran, field), field
    assert result.session_key == "one" and result.cost_usd is not None
    # One agent.run span per stream, with the TraceHook's run events.
    traced = [e["event"] for e in sink.events if e.get("trace_id") == result.trace_id]
    assert traced.count("run.started") == 1 and traced.count("run.completed") == 1


async def test_stream_deadline_and_budget(make_kernel) -> None:
    fake = FakeProvider(["late"], delay=5.0)
    kernel = make_kernel(fake)
    stream = kernel.agent(AgentSpec(name="slow")).stream("hi", deadline_s=0.2)
    events = [event async for event in stream]
    result = await stream.result()
    assert result.stop_reason == "deadline" and fake.calls[0].cancelled
    assert events[-1].type == "run.failed" and events[-1].result is result

    class _Refuse:
        def admit(self, estimate):
            raise BudgetExceeded("refused by test", call_id=estimate.call_id)

        def settle(self, reservation, event) -> None:
            pass

        def release(self, reservation) -> None:
            pass

    budgeted = make_kernel(FakeProvider(["x"]), budget=_Refuse())
    async with budgeted.agent(AgentSpec(name="b")).stream("hi") as stream:
        result = await stream.result()
    assert result.stop_reason == "budget" and isinstance(result.error, BudgetExceeded)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_stream_early_close_cancels_the_run(make_kernel) -> None:
    fake = FakeProvider(default="slow reply", delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="c"))
    async with agent.stream("hi") as stream:
        first = await stream.__anext__()
        assert first.type == "run.started"
        await _wait_for(lambda: bool(fake.calls))
    assert fake.calls[0].cancelled and not agent._runs
    with pytest.raises(RuntimeError, match="closed before it completed"):
        await stream.result()


async def test_abandoned_stream_is_finalised(make_kernel) -> None:
    fake = FakeProvider(default="slow reply", delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="a"))
    stream = agent.stream("hi")
    await stream.__anext__()
    await _wait_for(lambda: bool(fake.calls))
    del stream
    gc.collect()
    await _wait_for(lambda: fake.calls[0].cancelled and not agent._runs)


async def test_stream_of_closed_agent_run_is_cancelled(make_kernel) -> None:
    fake = FakeProvider(default="slow reply", delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="x"))
    stream = agent.stream("hi")
    await stream.__anext__()
    await _wait_for(lambda: bool(fake.calls))
    await agent.aclose()
    result = await stream.result()
    assert result.stop_reason == "cancelled"


def test_stream_sync_from_a_thread(make_kernel) -> None:
    kernel = make_kernel(FakeProvider([LIST_DIR, "done here"]))
    agent = kernel.agent(AgentSpec(name="t", limits=RunLimits(max_iterations=5)))
    out: dict[str, Any] = {}

    def consume() -> None:
        with agent.stream_sync("go") as stream:
            out["types"] = [event.type for event in stream]
            out["result"] = stream.result()

    thread = threading.Thread(target=consume)
    thread.start()
    thread.join(30)
    assert out["types"][0] == "run.started" and out["types"][-1] == "run.completed"
    assert out["result"].content == "done here"


def test_stream_sync_close_cancels(make_kernel) -> None:
    fake = FakeProvider(default="slow reply", delay=5.0)
    kernel = make_kernel(fake)
    agent = kernel.agent(AgentSpec(name="c"))
    stream = agent.stream_sync("hi")
    assert next(stream).type == "run.started"
    stream.close()
    assert not agent._runs
    assert not fake.calls or fake.calls[0].cancelled
