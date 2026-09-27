"""Typed calls (design 5b, Task 21): a tool result is checked before it is used.

- A tool declares ``Tool.output_schema`` (a JSON Schema dict; ``None`` by default).
  :func:`validate_result` runs after ``execute`` and before the result reaches a hook,
  the runner or the model, on every call path (``AgentRunner._run_tool``,
  ``ToolRegistry.execute``, ``nanobot.agent.tools.execution``). It is re-exported from
  ``nanobot.kernel.gate``, the module that owns the other per-call checks.
- No ``output_schema``, or an empty one (``{}``, which accepts any value): the result
  passes unchanged (the same object). Error results (``ToolResult.error``) are never
  validated. A ``ToolResult`` whose ``structured`` is an instance of the tool's own
  pydantic ``output_model`` (``FunctionTool``) is trusted: pydantic already checked it.
- JSON semantics: a ``type`` list is a union, and an integral float (``4.0``) satisfies
  ``integer``, as ``jsonschema`` and the MCP SDK agree.
- The value checked is, in order: a ``ToolResult``'s ``structured`` payload when it has
  one; else a ``str`` parsed as JSON (text that is not JSON fails); else the Python
  value itself (``dict``, ``list``, numbers, ``None``, a pydantic model dumped in JSON
  mode). The validator is ``Schema.schema_violations``, the one arguments already use.
- A failing result becomes ``ToolResult.error`` whose text carries
  :data:`RESULT_SCHEMA_MARKER` and each field path with what was expected
  (``temp.c: expected integer, got string``). Values are not quoted in the message,
  but field names are (an unexpected key under ``additionalProperties: false`` shows).
  A ``tool.result_invalid`` event goes to the trace sink.
- Classification: its own class, NOT a gate denial. The call was allowed and ran; the
  service returned data that breaks its own contract. So: no deferred-log entry (the
  "do not retry" note would be wrong: a retry may legitimately succeed), no
  ``violation:*`` signature and no count toward the I5 policy-denial ceiling. The runner
  treats it like any other tool error (``on_execute_tool_error``, retry hint).
- P5 (artifact store) is not built; the contract it can rely on is: a non-error result
  of a tool with an ``output_schema`` has passed this check.

``FunctionTool(output_model=)`` validates through ``nanobot.api.complete._coerce_json``,
the one JSON-value validation path ``acomplete_json`` and the solvers already use
(imported lazily there: ``nanobot.api.complete`` cannot import a kernel module at load
time without a cycle through ``nanobot.core``).

Module-level imports are stdlib, loguru and kernel only; agent modules are imported
lazily (Ruling C).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.kernel.trace import LoguruTraceSink, safe_emit

if TYPE_CHECKING:
    from nanobot.agent.tools.base import ToolResult
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.policy import Principal

RESULT_SCHEMA_MARKER = "result failed schema"

# The note every failure ends with: the model must not treat the payload as fact (I3).
_DISCARD_NOTE = "The result was discarded; do not treat it as fact."
_MAX_PROBLEMS = 20


def result_schema_error(tool_name: str, problems: list[str]) -> ToolResult:
    """The ``ToolResult.error`` for a result that failed its schema."""
    from nanobot.agent.tools.base import ToolResult

    shown = problems[:_MAX_PROBLEMS]
    more = len(problems) - len(shown)
    detail = "; ".join(shown) + (f"; ... and {more} more" if more > 0 else "")
    return ToolResult.error(
        f"Error: tool '{tool_name}' {RESULT_SCHEMA_MARKER}: {detail}. {_DISCARD_NOTE}"
    )


def pydantic_problems(exc: Exception) -> list[str]:
    """``<path>: <msg>`` per pydantic error, paths in the ``a.b[0].c`` form."""
    errors = getattr(exc, "errors", None)
    try:
        items = errors() if callable(errors) else []
    except Exception:  # noqa: BLE001 - fall back to the type name
        items = []
    problems: list[str] = []
    for item in items:
        path = ""
        for part in item.get("loc", ()):
            path = f"{path}[{part}]" if isinstance(part, int) else (
                f"{path}.{part}" if path else str(part)
            )
        msg = "required field missing" if item.get("type") == "missing" else str(
            item.get("msg", "invalid")
        )
        problems.append(f"{path or '(value)'}: {msg}")
    return problems or [f"(value): {type(exc).__name__}"]


def _tool_name(tool: Any) -> str:
    try:
        return str(tool.name)
    except Exception:  # noqa: BLE001 - a broken name property must not break the check
        return type(tool).__name__


def _structured_value(result: Any) -> tuple[bool, Any]:
    """``(True, value)`` to validate, or ``(False, None)`` when the text is not JSON."""
    from nanobot.agent.tools.base import ToolResult

    if isinstance(result, ToolResult) and result.has_structured:
        value = result.structured
    elif isinstance(result, str):
        try:
            return True, json.loads(result)
        except (TypeError, ValueError, RecursionError):
            return False, None
    else:
        value = result
    dump = getattr(value, "model_dump", None)
    if callable(dump) and not isinstance(value, type):
        value = dump(mode="json")
    return True, value


def _validated_by_output_model(tool: Any, result: Any) -> bool:
    """True when *result* carries an instance of the tool's own pydantic ``output_model``.

    ``FunctionTool(output_model=)`` already validated it with pydantic; the kernel does
    not re-derive a check from its dumped (serialization) form, which can legitimately
    differ from the validation schema (a ``field_serializer``, ``inf``).
    """
    from nanobot.agent.tools.base import ToolResult

    if not (isinstance(result, ToolResult) and result.has_structured):
        return False
    try:
        model_cls = getattr(tool, "output_model", None)
    except Exception:  # noqa: BLE001 - no trusted model: validate normally
        return False
    return isinstance(model_cls, type) and isinstance(result.structured, model_cls)


def validate_result(
    tool: Any,
    result: Any,
    *,
    env: CoreEnvironment | None = None,
    principal: Principal | None = None,
) -> Any:
    """*result* unchanged when it satisfies *tool*'s ``output_schema``, else a marked error.

    See the module docstring for what is checked and how a failure is classified.
    """
    from nanobot.agent.tools.base import Schema, ToolResult

    if isinstance(result, ToolResult) and result.is_error:
        return result
    name = _tool_name(tool)
    try:
        schema = getattr(tool, "output_schema", None)
    except Exception as exc:  # noqa: BLE001 - a broken declaration fails closed
        logger.warning("tool {} output_schema raised {!r}; rejecting its result", name, exc)
        return _reject(name, ["(value): the tool's output_schema could not be read"],
                       env, principal)
    if schema is None:
        return result
    if isinstance(schema, dict) and not schema:
        # ``{}`` accepts any value (the ``Operation`` contract): prose passes too.
        return result
    if not isinstance(schema, dict):
        return _reject(name, ["(value): the tool's output_schema is not a JSON Schema object"],
                       env, principal)
    if _validated_by_output_model(tool, result):
        return result
    parsed, value = _structured_value(result)
    if not parsed:
        return _reject(name, ["(value): not valid JSON, expected a value matching the "
                              "declared output schema"], env, principal)
    try:
        violations = Schema.schema_violations(value, schema, integral_floats=True)
    except Exception as exc:  # noqa: BLE001 - a schema the validator cannot apply fails closed
        logger.warning("tool {} output_schema could not be applied: {!r}", name, exc)
        return _reject(name, [f"(value): the output schema could not be applied "
                              f"({type(exc).__name__})"], env, principal)
    if not violations:
        return result
    return _reject(name, [v.describe() for v in violations], env, principal)


def _reject(
    name: str,
    problems: list[str],
    env: CoreEnvironment | None,
    principal: Principal | None,
) -> ToolResult:
    error = result_schema_error(name, problems)
    sink = env.trace if env is not None else LoguruTraceSink()
    safe_emit(sink, {
        "event": "tool.result_invalid",
        "actor": principal.name if principal is not None else "agent",
        "principal_kind": principal.kind if principal is not None else "agent",
        "tool": name,
        "marker": RESULT_SCHEMA_MARKER,
        "error": "; ".join(problems)[:500],
    })
    return error


__all__ = [
    "RESULT_SCHEMA_MARKER",
    "pydantic_problems",
    "result_schema_error",
    "validate_result",
]
