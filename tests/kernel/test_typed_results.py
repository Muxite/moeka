"""Typed calls (Task 21, design 5b): a tool's RESULT is validated before it is used.

- ``Tool.output_schema`` (default ``None``: no validation, the result passes exactly as
  today). A tool that opts in must return a value matching it: a JSON string, a JSON
  value, or a ``ToolResult`` carrying ``structured``.
- ``nanobot.kernel.gate.validate_result`` runs after ``execute`` in every call path
  (``AgentRunner._run_tool``, ``ToolRegistry.execute``, ``tools.execution``). A result
  that fails becomes ``ToolResult.error`` with ``RESULT_SCHEMA_MARKER`` and the field
  paths, and it never reaches ``after_execute_tool`` as a success.
- It is its own class: not a gate denial (no deferred entry, no I5 count).
- Wired for MCP (``outputSchema``/``structuredContent``), ``FunctionTool(output_model=)``
  and kernel-plugin manifest ``operations``.
- Argument errors now also name each field path and the expected type.
"""

from __future__ import annotations

import json
import sys
import uuid
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.core.function_tool import FunctionTool
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.gate import validate_result
from nanobot.kernel.manifest import MANIFEST_FILENAME, PluginManifest, compute_version_hash
from nanobot.kernel.policy import Principal
from nanobot.kernel.registry import PluginRegistry
from nanobot.kernel.typed import RESULT_SCHEMA_MARKER
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.agent.runner_helpers import make_run_spec

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "temp": {"type": "object", "properties": {"c": {"type": "integer"}}, "required": ["c"]},
    },
    "required": ["city", "temp"],
}


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


def _env(tmp_path: Path, sink: Any = None) -> CoreEnvironment:
    return CoreEnvironment(
        config=_EmptyConfig(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink if sink is not None else RecordingSink(),
    )


class ReturnTool(Tool):
    """Returns whatever it was built with; optionally declares an output schema."""

    def __init__(self, value: Any, schema: dict[str, Any] | None = None, name: str = "svc"):
        self._value = value
        self._name = name
        if schema is not None:
            self.output_schema = schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "typed test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return self._value


class SpyHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def after_execute_tool(self, context, tool_call, tool, params, result) -> None:
        self.calls.append("after_execute_tool")

    async def on_execute_tool_error(self, context, tool_call, tool, params, error) -> None:
        self.calls.append("on_execute_tool_error")


def _provider(*calls: ToolCallRequest) -> MagicMock:
    provider = MagicMock()
    responses = [LLMResponse(content="calling", tool_calls=[c]) for c in calls]
    responses.append(LLMResponse(content="done", tool_calls=[]))
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    return provider


async def _run(tools: ToolRegistry, *calls: ToolCallRequest, **spec: Any):
    return await AgentRunner().run(make_run_spec(
        _provider(*calls),
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=len(calls) + 2,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        **spec,
    ))


def _tool_messages(result) -> list[str]:
    return [m["content"] for m in result.messages if m.get("role") == "tool"]


_VALID = json.dumps({"city": "Oslo", "temp": {"c": 4}})
_BAD = json.dumps({"city": "Oslo", "temp": {"c": "cold"}})


# -- the general mechanism ---------------------------------------------------------


@pytest.mark.parametrize("value", [
    "plain text, not json", "", {"any": "dict"}, ["block"], None, 42,
    ToolResult("tr text"), ToolResult.error("Error: failed"),
])
def test_no_output_schema_passes_every_result_unchanged(value):
    tool = ReturnTool(value)
    assert tool.output_schema is None
    out = validate_result(tool, value)
    assert out is value


def test_valid_json_result_passes_unchanged():
    tool = ReturnTool(_VALID, _SCHEMA)
    assert validate_result(tool, _VALID) is _VALID


def test_valid_structured_values_pass():
    tool = ReturnTool(None, _SCHEMA)
    value = {"city": "Oslo", "temp": {"c": 4}}
    assert validate_result(tool, value) is value
    tr = ToolResult("rendered", structured=value)
    assert validate_result(tool, tr) is tr


def test_violating_result_becomes_marked_error_with_field_path():
    tool = ReturnTool(_BAD, _SCHEMA)
    out = validate_result(tool, _BAD)
    assert is_tool_error_result(out)
    assert out.startswith("Error")
    assert RESULT_SCHEMA_MARKER in out
    assert "temp.c: expected integer" in out
    assert "cold" not in out, "the rejected payload is never echoed as fact"


def test_missing_required_field_is_named():
    tool = ReturnTool(None, _SCHEMA)
    out = validate_result(tool, json.dumps({"city": "Oslo"}))
    assert RESULT_SCHEMA_MARKER in out and "temp: required field missing" in out


@pytest.mark.parametrize("text", ["sunny and warm", "{not json", ""])
def test_non_json_result_fails_cleanly(text):
    tool = ReturnTool(text, _SCHEMA)
    out = validate_result(tool, text)
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "not valid JSON" in out


def test_error_results_are_not_validated():
    err = ToolResult.error("Error: upstream down")
    assert validate_result(ReturnTool(err, _SCHEMA), err) is err


def test_structured_payload_wins_over_text():
    """A ToolResult's ``structured`` is what gets validated, not its display text."""
    tool = ReturnTool(None, _SCHEMA)
    good_text_bad_payload = ToolResult(_VALID, structured={"city": 1})
    out = validate_result(tool, good_text_bad_payload)
    assert RESULT_SCHEMA_MARKER in out and "city: expected string" in out


def test_result_failure_emits_trace_event(tmp_path):
    sink = RecordingSink()
    validate_result(ReturnTool(_BAD, _SCHEMA), _BAD, env=_env(tmp_path, sink))
    events = [e for e in sink.events if e["event"] == "tool.result_invalid"]
    assert len(events) == 1 and events[0]["tool"] == "svc"
    assert "temp.c" in events[0]["error"]


def test_broken_output_schema_fails_closed():
    class Broken(ReturnTool):
        @property
        def output_schema(self):  # type: ignore[override]
            raise RuntimeError("boom")

    out = validate_result(Broken(_VALID), _VALID)
    assert is_tool_error_result(out) and RESULT_SCHEMA_MARKER in out


# -- every call path -----------------------------------------------------------------


async def test_runner_path_rejects_bad_result_before_success_hook(tmp_path):
    tools = ToolRegistry()
    tools.register(ReturnTool(_BAD, _SCHEMA))
    hook = SpyHook()
    result = await _run(
        tools, ToolCallRequest(id="c1", name="svc", arguments={}),
        hook=hook, env=_env(tmp_path),
    )
    msg = _tool_messages(result)[0]
    assert RESULT_SCHEMA_MARKER in msg and "temp.c: expected integer" in msg
    assert result.tool_events[0]["status"] == "error"
    assert hook.calls == ["on_execute_tool_error"]
    # Its own class: not a gate denial, so no deferred entry and no policy count.
    from nanobot.kernel.deferred import DEFERRED_NOTE, deferred_log_path

    assert DEFERRED_NOTE not in msg
    assert not deferred_log_path(tmp_path / "work").exists()


async def test_runner_path_passes_valid_result(tmp_path):
    tools = ToolRegistry()
    tools.register(ReturnTool(_VALID, _SCHEMA))
    hook = SpyHook()
    result = await _run(
        tools, ToolCallRequest(id="c1", name="svc", arguments={}),
        hook=hook, env=_env(tmp_path),
    )
    assert _tool_messages(result)[0] == _VALID
    assert result.tool_events[0]["status"] == "ok"
    assert hook.calls == ["after_execute_tool"]


async def test_registry_execute_path_validates():
    tools = ToolRegistry()
    tools.register(ReturnTool(_BAD, _SCHEMA))
    tools.register(ReturnTool(_VALID, _SCHEMA, name="good"))
    bad = await tools.execute("svc", {})
    assert is_tool_error_result(bad) and RESULT_SCHEMA_MARKER in bad
    assert await tools.execute("good", {}) == _VALID


async def test_execution_module_path_validates():
    from nanobot.agent.tools.execution import execute_tool_calls

    tools = ToolRegistry()
    tools.register(ReturnTool(_BAD, _SCHEMA))
    hook = SpyHook()
    results, events = await execute_tool_calls(
        tools,
        [ToolCallRequest(id="c1", name="svc", arguments={})],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=hook,
        context=AgentHookContext(iteration=0, messages=[]),
    )
    assert RESULT_SCHEMA_MARKER in results[0]
    assert events[0]["status"] == "error"
    assert hook.calls == ["on_execute_tool_error"]


async def test_untyped_tool_unchanged_on_every_path(tmp_path):
    """Regression boundary: a tool without output_schema behaves exactly as before."""
    tools = ToolRegistry()
    tools.register(ReturnTool("not json at all"))
    assert await tools.execute("svc", {}) == "not json at all"
    result = await _run(tools, ToolCallRequest(id="c1", name="svc", arguments={}),
                        env=_env(tmp_path))
    assert _tool_messages(result)[0] == "not json at all"
    assert result.tool_events[0]["status"] == "ok"

    from nanobot.agent.tools.execution import execute_tool_calls

    results, events = await execute_tool_calls(
        tools,
        [ToolCallRequest(id="c2", name="svc", arguments={})],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=SpyHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )
    assert results == ["not json at all"] and events[0]["status"] == "ok"


# -- review fix round 1: JSON Schema semantics the SDK already accepts -----------------


@pytest.mark.parametrize("value", ["plain prose", "", {"a": 1}, 3])
def test_empty_output_schema_accepts_anything_including_prose(value):
    """``{}`` accepts any value (the ``Operation`` contract), prose included."""
    assert validate_result(ReturnTool(value, {}), value) is value


async def test_kernel_plugin_operation_with_empty_output_schema_passes_prose(
    tmp_path, plugin_base,
):
    op = {**_OP, "output_schema": {}}
    registry = _load_plugin(tmp_path, _OpPlugin(plugin_base, [op]), kernel=True)
    assert registry.get("forecast").output_schema == {}
    assert await registry.execute("forecast", {"out": "sunny, no json"}) == "sunny, no json"


def test_type_list_is_a_union():
    schema = {"type": "object", "properties": {"v": {"type": ["string", "integer"]}}}
    tool = ReturnTool(None, schema)
    for good in ({"v": "five"}, {"v": 5}):
        assert validate_result(tool, good) is good
    out = validate_result(tool, {"v": [5]})
    assert RESULT_SCHEMA_MARKER in out and "v: expected string or integer, got array" in out
    # A union member's own keywords still apply once it matched.
    bounded = {"type": ["string", "integer"], "minimum": 10}
    assert "expected >= 10" in validate_result(ReturnTool(None, bounded), 5)
    # Nullable unions keep working, and the legacy argument message is unchanged.
    from nanobot.agent.tools.base import Schema

    assert Schema.validate_json_schema_value(None, {"type": ["string", "integer", "null"]}) == []
    assert Schema.validate_json_schema_value([1], {"type": ["string", "integer"]}, "v") == [
        "v should be string"
    ]


def test_integral_float_satisfies_integer_in_results():
    tool = ReturnTool(None, _SCHEMA)
    value = {"city": "Oslo", "temp": {"c": 4.0}}
    assert validate_result(tool, value) is value
    assert validate_result(tool, json.dumps(value)) == json.dumps(value)
    assert "temp.c: expected integer, got number" in validate_result(
        tool, {"city": "Oslo", "temp": {"c": 4.5}},
    )


def test_integral_float_still_rejected_for_arguments():
    """Arguments keep the strict check: a tool never receives 4.0 for an integer param."""
    from nanobot.agent.tools.base import Schema

    assert Schema.validate_json_schema_value(4.0, {"type": "integer"}, "n") == [
        "n should be integer"
    ]


def test_deeply_nested_json_text_fails_with_the_marker():
    text = "[" * 100000 + "]" * 100000
    out = validate_result(ReturnTool(text, {"type": "array"}), text)
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "not valid JSON" in out


async def test_function_tool_field_serializer_is_not_rejected_by_recheck():
    from pydantic import field_serializer

    class Count(BaseModel):
        n: int

        @field_serializer("n")
        def _as_text(self, v: int) -> str:
            return str(v)

    tool = FunctionTool(lambda: {"n": 3}, name="cnt", output_model=Count)
    tools = ToolRegistry()
    tools.register(tool)
    out = await tools.execute("cnt", {})
    assert not is_tool_error_result(out), out
    assert json.loads(out) == {"n": "3"}


# -- structured argument errors ---------------------------------------------------------


class NestedArgsTool(ReturnTool):
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"a": {"type": "object", "properties": {"b": {
                "type": "object", "properties": {"c": {"type": "integer"}},
            }}}, "mode": {"type": "string", "enum": ["x", "y"]}},
            "required": ["mode"],
        }


def test_argument_error_names_field_path_and_expected_type():
    tools = ToolRegistry()
    tools.register(NestedArgsTool("ok", name="nested"))
    _tool, _params, error = tools.prepare_call("nested", {"a": {"b": {"c": "many"}}})
    assert error is not None
    # The legacy message is kept (pinned elsewhere) ...
    assert "a.b.c should be integer" in error and "missing required mode" in error
    # ... and each bad field is named with its expected type.
    assert "a.b.c: expected integer" in error
    assert "mode: required field missing" in error


def test_argument_violations_are_structured():
    from nanobot.agent.tools.base import Schema

    violations = Schema.schema_violations(
        {"a": {"b": {"c": "x"}}, "mode": "z"}, NestedArgsTool("").parameters,
    )
    described = [v.describe() for v in violations]
    assert "a.b.c: expected integer, got string" in described
    assert any(d.startswith("mode: expected one of") for d in described)
    # The legacy list is the same violations' messages, unchanged.
    assert Schema.validate_json_schema_value(
        {"a": {"b": {"c": "x"}}, "mode": "z"}, NestedArgsTool("").parameters,
    ) == [v.message for v in violations]


# -- MCP ----------------------------------------------------------------------------------


class _Session:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.exc is not None:
            raise self.exc
        return self.result


def _mcp(result: Any = None, *, output_schema: dict[str, Any] | None = None,
         exc: Exception | None = None) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="weather", description="weather", inputSchema={"type": "object", "properties": {}},
    )
    if output_schema is not None:
        tool_def.outputSchema = output_schema
    return MCPToolWrapper(_Session(result, exc), "svc", tool_def)


def _call_result(text: str, structured: Any = None, *, has_structured: bool = True):
    from mcp import types

    fields: dict[str, Any] = {"content": [types.TextContent(type="text", text=text)]}
    if has_structured:
        fields["structuredContent"] = structured
    return SimpleNamespace(isError=False, **fields)


async def _mcp_call(wrapper: MCPToolWrapper) -> Any:
    tools = ToolRegistry()
    tools.register(wrapper)
    return await tools.execute(wrapper.name, {})


async def test_mcp_structured_and_schema_valid_passes():
    payload = {"city": "Oslo", "temp": {"c": 4}}
    wrapper = _mcp(_call_result(json.dumps(payload), payload), output_schema=_SCHEMA)
    assert wrapper.output_schema is not None
    out = await _mcp_call(wrapper)
    assert not is_tool_error_result(out)
    assert json.dumps(payload) in out  # the rendered (bannered) text reaches the model
    direct = await wrapper.execute()
    assert direct.structured == payload


async def test_mcp_structured_and_schema_invalid_fails_with_marker():
    payload = {"city": "Oslo", "temp": {"c": "cold"}}
    wrapper = _mcp(_call_result(json.dumps(payload), payload), output_schema=_SCHEMA)
    out = await _mcp_call(wrapper)
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "temp.c: expected integer" in out


async def test_mcp_schema_without_structured_content_fails_with_marker():
    """outputSchema declared but no structuredContent: the MCP spec says the server MUST
    return it (the SDK raises too), so the result is rejected, not parsed from text."""
    wrapper = _mcp(_call_result(_VALID, has_structured=False), output_schema=_SCHEMA)
    out = await _mcp_call(wrapper)
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "no structuredContent" in out


async def test_mcp_structured_without_schema_is_legacy_text():
    """structuredContent but no outputSchema: nothing to check against; text as today."""
    plain = _mcp(_call_result("hello", has_structured=False))
    with_sc = _mcp(_call_result("hello", {"x": 1}))
    assert with_sc.output_schema is None
    assert await _mcp_call(with_sc) == await _mcp_call(plain)


async def test_mcp_neither_is_legacy_behavior():
    wrapper = _mcp(_call_result("hello", has_structured=False))
    assert wrapper.output_schema is None
    out = await _mcp_call(wrapper)
    assert not is_tool_error_result(out) and out.endswith("hello")
    assert type(await wrapper.execute()) is str


@pytest.mark.parametrize("message", [
    "Tool weather has an output schema but did not return structured content",
    "Invalid structured content returned by tool weather: 'c' is a required property",
])
async def test_mcp_sdk_result_validation_errors_carry_the_marker(message):
    """The real SDK validates in call_tool and raises RuntimeError; keep the class."""
    wrapper = _mcp(output_schema=_SCHEMA, exc=RuntimeError(message))
    out = await wrapper.execute()
    assert is_tool_error_result(out) and RESULT_SCHEMA_MARKER in out


async def test_mcp_is_error_result_not_validated():
    res = _call_result("boom", has_structured=False)
    res.isError = True
    out = await _mcp_call(_mcp(res, output_schema=_SCHEMA))
    assert is_tool_error_result(out) and RESULT_SCHEMA_MARKER not in out


# -- FunctionTool(output_model=) ------------------------------------------------------


class Temp(BaseModel):
    c: int


class Weather(BaseModel):
    city: str
    temp: Temp


async def test_function_tool_output_model_coerces_valid_dict():
    tool = FunctionTool(lambda: {"city": "Oslo", "temp": {"c": "4"}}, name="w",
                        output_model=Weather)
    assert tool.output_schema is not None
    out = await tool.execute()
    assert isinstance(out.structured, Weather) and out.structured.temp.c == 4
    assert json.loads(out) == {"city": "Oslo", "temp": {"c": 4}}
    tools = ToolRegistry()
    tools.register(tool)
    assert not is_tool_error_result(await tools.execute("w", {}))


async def test_function_tool_output_model_invalid_is_error_not_crash():
    tool = FunctionTool(lambda: {"city": "Oslo", "temp": {"c": "cold"}}, name="w",
                        output_model=Weather)
    out = await tool.execute()
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "temp.c" in out


async def test_function_tool_output_model_accepts_json_text_and_rejects_prose():
    good = FunctionTool(lambda: _VALID, name="w", output_model=Weather)
    assert isinstance((await good.execute()).structured, Weather)
    bad = FunctionTool(lambda: "it is sunny", name="w", output_model=Weather)
    out = await bad.execute()
    assert is_tool_error_result(out) and "not valid JSON" in out


async def test_function_tool_without_output_model_unchanged():
    tool = FunctionTool(lambda: {"a": 1}, name="w")
    assert tool.output_schema is None
    out = await tool.execute()
    assert out == "{'a': 1}" and type(out) is str


async def test_function_tool_uses_the_shared_coercion_path(monkeypatch):
    """One validation path for JSON values: acomplete_json, solvers and FunctionTool."""
    import importlib

    complete = importlib.import_module("nanobot.api.complete")
    seen: list[Any] = []
    real = complete._coerce_json

    def spy(parsed: Any, model_cls: type | None) -> Any:
        seen.append((parsed, model_cls))
        return real(parsed, model_cls)

    monkeypatch.setattr(complete, "_coerce_json", spy)
    tool = FunctionTool(lambda: {"city": "Oslo", "temp": {"c": 4}}, name="w",
                        output_model=Weather)
    await tool.execute()
    assert seen == [({"city": "Oslo", "temp": {"c": 4}}, Weather)]


# -- manifest operations (kernel-mode plugins) ------------------------------------------

HOST = Principal("host", "host")

_PLUGIN_SRC = '''
from typing import Any

from nanobot.agent.tools.base import Tool


class OpTool(Tool):
    @property
    def name(self) -> str:
        return "forecast"

    @property
    def description(self) -> str:
        return "typed service plugin"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"out": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs.get("out", "")
'''

_OP = {
    "name": "forecast",
    "input_schema": {"type": "object", "properties": {"out": {"type": "string"}},
                     "required": ["out"]},
    "output_schema": {"type": "object", "properties": {"n": {"type": "integer"}},
                      "required": ["n"]},
}


class _OpPlugin:
    def __init__(self, base: Path, operations: list[dict[str, Any]]) -> None:
        self.pkg = f"kplug_{uuid.uuid4().hex[:10]}"
        root = base / self.pkg
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        (root / "tool.py").write_text(_PLUGIN_SRC, encoding="utf-8")
        self.value = f"{self.pkg}.tool:OpTool"
        raw: dict[str, Any] = {
            "name": "weatherplug", "kind": "tool", "version": "1.0.0",
            "version_hash": "0" * 64, "tier": 2, "entry": self.value,
            "operations": operations,
        }
        raw["version_hash"] = compute_version_hash(root, raw)
        (root / MANIFEST_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
        self.root = root
        self.manifest = PluginManifest.model_validate(raw)

    def entry_point(self) -> EntryPoint:
        return EntryPoint(name="weatherplug", value=self.value, group="nanobot.tools")


@pytest.fixture
def plugin_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "site"
    base.mkdir()
    monkeypatch.syspath_prepend(str(base))
    before = set(sys.modules)
    yield base
    for mod in set(sys.modules) - before:
        if mod.startswith("kplug_"):
            sys.modules.pop(mod, None)


def _load_plugin(tmp_path: Path, plugin: _OpPlugin, *, kernel: bool) -> ToolRegistry:
    env = _env(tmp_path)
    (tmp_path / "work").mkdir(exist_ok=True)
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path / "work"), env=env)
    if kernel:
        plugin_registry = PluginRegistry(tmp_path / "state", trace=RecordingSink())
        plugin_registry.register(plugin.manifest, plugin.root, principal=HOST)
        plugin_registry.activate("weatherplug", plugin.manifest.version_hash, HOST)
        loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)
    else:
        loader = ToolLoader(test_classes=[])
    registry = ToolRegistry()
    with patch("nanobot.agent.tools.loader.entry_points", return_value=[plugin.entry_point()]):
        loader.load(ctx, registry)
    assert registry.has("forecast")
    return registry


async def test_kernel_plugin_operation_output_schema_is_enforced(tmp_path, plugin_base):
    registry = _load_plugin(tmp_path, _OpPlugin(plugin_base, [_OP]), kernel=True)
    tool = registry.get("forecast")
    assert tool.output_schema == _OP["output_schema"]

    assert await registry.execute("forecast", {"out": '{"n": 3}'}) == '{"n": 3}'
    bad = await registry.execute("forecast", {"out": '{"n": "three"}'})
    assert is_tool_error_result(bad) and RESULT_SCHEMA_MARKER in bad
    assert "n: expected integer" in bad


async def test_kernel_plugin_operation_input_schema_is_enforced(tmp_path, plugin_base):
    registry = _load_plugin(tmp_path, _OpPlugin(plugin_base, [_OP]), kernel=True)
    _tool, _params, error = registry.prepare_call("forecast", {})
    assert error is not None and "out: required field missing" in error


async def test_kernel_plugin_without_matching_operation_is_untyped(tmp_path, plugin_base):
    other = {**_OP, "name": "something_else"}
    registry = _load_plugin(tmp_path, _OpPlugin(plugin_base, [other]), kernel=True)
    assert registry.get("forecast").output_schema is None
    assert await registry.execute("forecast", {"out": "prose"}) == "prose"


async def test_legacy_mode_plugin_ignores_manifest_operations(tmp_path, plugin_base):
    registry = _load_plugin(tmp_path, _OpPlugin(plugin_base, [_OP]), kernel=False)
    assert registry.get("forecast").output_schema is None
    assert await registry.execute("forecast", {"out": '{"n": "three"}'}) == '{"n": "three"}'


def test_builtin_tools_declare_no_output_schema():
    classes = ToolLoader().discover()
    assert classes
    assert [c.__name__ for c in classes if getattr(c, "output_schema", None) is not None] == []


# -- the marker ------------------------------------------------------------------------


def test_marker_collides_with_no_other_marker():
    from nanobot.agent.tools.execution import _SSRF_MARKERS, _WORKSPACE_VIOLATION_MARKERS
    from nanobot.kernel.deferred import DEFERRED_NOTE
    from nanobot.kernel.floors import _EXEC_FLOOR_MARKER, PLUGIN_LOAD_MARKER
    from nanobot.kernel.policy import POLICY_MARKER
    from nanobot.kernel.strict import STRICT_SANDBOX_MARKER
    from nanobot.security.protected_paths import PROTECTED_MARKER
    from nanobot.security.untrusted import UNTRUSTED_BANNER
    from nanobot.utils.runtime import (
        _EXEC_FLOOR_TEXT,
        _EXEC_GUARD_MARKERS,
        _PROTECTED_PATH_MARKER,
    )

    marker = RESULT_SCHEMA_MARKER.lower()
    others = [
        POLICY_MARKER, PROTECTED_MARKER, STRICT_SANDBOX_MARKER, PLUGIN_LOAD_MARKER,
        _EXEC_FLOOR_MARKER, DEFERRED_NOTE, UNTRUSTED_BANNER, _EXEC_FLOOR_TEXT,
        _PROTECTED_PATH_MARKER, "repeated external lookup blocked",
        *(m for m, _ in _EXEC_GUARD_MARKERS),
        *_SSRF_MARKERS, *_WORKSPACE_VIOLATION_MARKERS,
        *AgentRunner._SSRF_MARKERS, *AgentRunner._WORKSPACE_VIOLATION_MARKERS,
    ]
    for other in others:
        assert other.lower() not in marker and marker not in other.lower(), other


def test_result_error_is_not_classified_as_a_violation():
    runner = AgentRunner()
    out = validate_result(ReturnTool(_BAD, _SCHEMA), _BAD)
    counts: dict[str, int] = {}
    handled = runner._classify_violation(
        raw_text=out, soft_payload=out, event={},
        tool_call=ToolCallRequest(id="c", name="svc", arguments={}),
        workspace_violation_counts=counts,
    )
    assert handled is None and counts == {}


# -- review fix round 2 ------------------------------------------------------------------


class _Count(BaseModel):
    n: int


@pytest.mark.parametrize("make", [
    lambda: _Count.model_construct(n="not-int"),
    lambda: _mutated(),
])
async def test_function_tool_unvalidated_instance_is_rejected(make):
    """pydantic returns an instance as is; FunctionTool re-validates its fields."""
    tool = FunctionTool(make, name="cnt", output_model=_Count)
    tools = ToolRegistry()
    tools.register(tool)
    out = await tools.execute("cnt", {})
    assert is_tool_error_result(out)
    assert RESULT_SCHEMA_MARKER in out and "n:" in out


def _mutated() -> _Count:
    inst = _Count(n=1)
    inst.n = "not-int"  # no validate_assignment: pydantic does not check this
    return inst


async def test_function_tool_valid_instance_still_passes():
    tool = FunctionTool(lambda: _Count(n=2), name="cnt", output_model=_Count)
    out = await tool.execute()
    assert not is_tool_error_result(out) and out.structured == _Count(n=2)


def test_only_function_tool_may_skip_the_recheck():
    """A tool mimicking FunctionTool's shape cannot opt out of its output_schema."""

    class Mimic(ReturnTool):
        output_model = _Count

    bad = ToolResult('{"n": "x"}', structured=_Count.model_construct(n="x"))
    tool = Mimic(bad, {"type": "object", "properties": {"n": {"type": "integer"}}})
    out = validate_result(tool, bad)
    assert is_tool_error_result(out) and "n: expected integer, got string" in out


def test_nullable_union_accepts_none_for_arguments_and_results():
    from nanobot.agent.tools.base import Schema

    schema = {"type": ["string", "integer"], "nullable": True}
    assert Schema.validate_json_schema_value(None, schema, "v") == []
    wrapped = {"type": "object", "properties": {"v": schema}}
    assert Schema.validate_json_schema_value({"v": None}, wrapped) == []
    value = {"v": None}
    assert validate_result(ReturnTool(None, wrapped), value) is value
    # Not nullable: None is still rejected.
    strict = {"type": "object", "properties": {"v": {"type": ["string", "integer"]}}}
    assert RESULT_SCHEMA_MARKER in validate_result(ReturnTool(None, strict), {"v": None})


def test_union_types_apply_to_arguments_too():
    """Deliberate: a type list is a union for arguments as well (JSON Schema semantics)."""
    from nanobot.agent.tools.base import Schema

    assert Schema.validate_json_schema_value("abc", {"type": ["integer", "string"]}) == []


# -- review fix round 3: valid instances pass (re-validated by field values) -------------


async def _typed_out(fn, model) -> Any:
    tool = FunctionTool(fn, name="m", output_model=model)
    tools = ToolRegistry()
    tools.register(tool)
    return await tools.execute("m", {})


async def test_valid_instance_with_alias_passes():
    from pydantic import Field

    class Aliased(BaseModel):
        n: int = Field(alias="N")

    out = await _typed_out(lambda: Aliased(N=3), Aliased)
    assert not is_tool_error_result(out), out


async def test_valid_instance_with_excluded_required_field_passes():
    from pydantic import Field

    class Excl(BaseModel):
        n: int
        secret: str = Field(exclude=True)

    out = await _typed_out(lambda: Excl(n=1, secret="x"), Excl)
    assert not is_tool_error_result(out), out
    assert "secret" not in out


async def test_valid_instance_with_value_changing_serializer_passes():
    from pydantic import field_serializer

    class Items(BaseModel):
        n: int

        @field_serializer("n")
        def _items(self, v: int) -> str:
            return f"{v} items"

    out = await _typed_out(lambda: Items(n=3), Items)
    assert not is_tool_error_result(out), out
    assert json.loads(out) == {"n": "3 items"}


async def test_nested_constructed_instance_is_rejected():
    class Outer(BaseModel):
        inner: _Count

    out = await _typed_out(
        lambda: Outer(inner=_Count(n=1)).model_copy(
            update={"inner": _Count.model_construct(n="bad")},
        ),
        Outer,
    )
    assert is_tool_error_result(out) and RESULT_SCHEMA_MARKER in out and "inner.n" in out
