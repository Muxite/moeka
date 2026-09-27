"""Adapt a plain Python callable into a moeka :class:`Tool`.

This is the bridge that lets a host application "connect actions in code" to the
agent: any sync or async function becomes a tool the LLM can call, with its JSON
Schema derived from type hints (or supplied explicitly).
"""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from typing import Any, get_args, get_origin, get_type_hints

from nanobot.agent.tools.base import Tool, ToolResult

if typing.TYPE_CHECKING:
    from pydantic import BaseModel

# Python annotation -> JSON Schema "type" string.
_PY_TO_JSON: dict[type, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    list: "array",
    dict: "object",
}


def _json_type_for(annotation: Any) -> dict[str, Any]:
    """Map a Python annotation to a JSON Schema fragment.

    Handles bare builtins, ``Optional[X]`` / ``X | None`` (marks nullable), and
    parameterized ``list[T]`` (emits an ``items`` fragment). Unknown annotations
    fall back to an unconstrained schema (no ``type``), which validates anything.
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)

    # Optional[X] / Union[X, None] / X | None -> nullable fragment of non-None arm.
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        has_none = len(get_args(annotation)) != len(args)
        if len(args) == 1:
            frag = _json_type_for(args[0])
        else:
            frag = {}  # genuine multi-type union -> unconstrained
        if has_none and "type" in frag:
            frag["type"] = [frag["type"], "null"]
        return frag

    # Parameterized containers: list[T], dict[K, V].
    if origin in (list, set, tuple):
        frag: dict[str, Any] = {"type": "array"}
        item_args = get_args(annotation)
        if item_args:
            inner = _json_type_for(item_args[0])
            if inner:
                frag["items"] = inner
        return frag
    if origin is dict:
        return {"type": "object"}

    if isinstance(annotation, type):
        # Enum -> enum of member values.
        if issubclass(annotation, __import__("enum").Enum):
            return {"enum": [m.value for m in annotation]}
        json_type = _PY_TO_JSON.get(annotation)
        if json_type:
            return {"type": json_type}

    return {}


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build a JSON-Schema ``parameters`` object from a callable's signature.

    Required params are those without a default. ``*args``/``**kwargs`` and a
    leading ``self``/``cls`` are skipped. Untyped callables yield an empty
    properties object (the LLM may pass anything).
    """
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        properties[name] = _json_type_for(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


class FunctionTool(Tool):
    """Adapts a plain (sync or async) Python callable into a moeka Tool.

    Instances are created dynamically by a host (not auto-discovered), so
    ``_plugin_discoverable`` is False. The callable is invoked with validated,
    cast keyword arguments and its return value is passed straight back to the
    agent loop (string or content blocks).

    ``output_model`` (optional, typed calls, design 5b): a pydantic model the return
    value must validate against. A dict, a model instance or JSON text is validated
    through ``nanobot.api.complete._coerce_json`` (the same path ``acomplete_json``
    uses) and returned as ``ToolResult(<model JSON>, structured=<model instance>)``;
    a mismatch returns ``ToolResult.error`` with ``RESULT_SCHEMA_MARKER`` and the field
    paths, never raises. ``output_schema`` is then the model's JSON Schema (it declares
    the tool as typed); the kernel's ``validate_result`` trusts the pydantic instance
    rather than re-checking its dumped form, which a ``field_serializer`` may change.
    Without it the return value passes exactly as before.
    """

    _plugin_discoverable = False

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        read_only: bool = False,
        output_model: type[BaseModel] | None = None,
    ) -> None:
        if not callable(fn):
            raise TypeError(f"FunctionTool expects a callable, got {type(fn).__name__}")
        self._fn = fn
        self._name = name or getattr(fn, "__name__", None) or "anonymous_action"
        doc = inspect.getdoc(fn) or ""
        self._description = description or doc.split("\n\n")[0].strip() or self._name
        self._parameters = parameters if parameters is not None else _schema_from_signature(fn)
        self._read_only = read_only
        self._output_model = output_model
        if output_model is not None:
            self.output_schema = output_model.model_json_schema()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def output_model(self) -> type[BaseModel] | None:
        """The pydantic model results are validated with (``None``: untyped)."""
        return self._output_model

    async def execute(self, **kwargs: Any) -> Any:
        result = self._fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if self._output_model is not None:
            return self._typed_result(result)
        return result if isinstance(result, (str, list)) else str(result)

    def _typed_result(self, result: Any) -> ToolResult:
        """Validate *result* against ``output_model`` (see the class docstring)."""
        import json

        from nanobot.api.complete import _coerce_json  # lazy: api imports core
        from nanobot.kernel.typed import pydantic_problems, result_schema_error

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return result_schema_error(self._name, [
                    "(value): not valid JSON, expected a value matching the declared "
                    "output schema",
                ])
        try:
            model = _coerce_json(result, self._output_model)
        except Exception as exc:  # noqa: BLE001 - a bad return value is a tool error
            return result_schema_error(self._name, pydantic_problems(exc))
        return ToolResult(model.model_dump_json(), structured=model)
