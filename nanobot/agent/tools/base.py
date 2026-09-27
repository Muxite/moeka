"""Base class for agent tools."""
from __future__ import annotations

import functools
import inspect
import math
import re
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar, cast

if typing.TYPE_CHECKING:
    from pydantic import BaseModel

    from nanobot.agent.tools.context import ToolContext
    from nanobot.kernel.policy import CapabilityRequest
    from nanobot.runtime_context import RuntimeContextProvider

_ToolT = TypeVar("_ToolT", bound="Tool")


def capability_request(capability: str, resource: Any = "") -> CapabilityRequest:
    """Build a ``CapabilityRequest`` for ``Tool.capabilities`` declarations.

    ``nanobot.kernel.policy`` is imported lazily: importing any ``nanobot.kernel``
    submodule runs ``nanobot/kernel/__init__.py``, which re-exports ``nanobot.core`` and
    so imports this module back (a module-level import here is a cycle).
    Non-string resources (a malformed tool call) are coerced with ``str``; ``None``
    becomes ``""``.
    """
    from nanobot.kernel.policy import CapabilityRequest

    if resource is None:
        resource = ""
    elif not isinstance(resource, str):
        resource = str(resource)
    return CapabilityRequest(capability, resource)

# -- descriptions as data (Task 20) ------------------------------------------------
#
# - A built-in tool's description lives in ``descriptions/<tool name>.txt`` next to this
#   module, read through :class:`description_from_file`. The file is UTF-8; its text is
#   the file minus exactly one trailing newline (POSIX text files end with one).
# - Descriptions built at runtime stay Python: ``exec`` (platform branch) and ``my``
#   (``allow_set`` config). ``parameters`` schemas stay Python: they are bound to runtime
#   constants and platform branches, and their key order is provider-visible.
# - The golden snapshot ``tests/kernel/test_description_golden.py`` pins every
#   description and parameters schema byte-for-byte, whatever its source.

DESCRIPTIONS_DIR = Path(__file__).parent / "descriptions"
_DESCRIPTION_NAME = re.compile(r"[a-z][a-z0-9_]*")


class DescriptionFileError(RuntimeError):
    """A tool description data file is missing, unreadable or not UTF-8."""


def read_description_file(path: Path, *, tool: str = "") -> str:
    """Text of a description file: UTF-8, with exactly one trailing newline removed."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise DescriptionFileError(
            f"description file for tool {tool!r} is unreadable: {path} ({reason})"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DescriptionFileError(
            f"description file for tool {tool!r} is not valid UTF-8: {path}"
        ) from exc
    return text[:-1] if text.endswith("\n") else text


@functools.cache
def builtin_description(name: str) -> str:
    """The built-in description ``descriptions/<name>.txt``, read once per process."""
    return read_description_file(DESCRIPTIONS_DIR / f"{name}.txt", tool=name)


class description_from_file:  # noqa: N801 - used in place of ``@property``
    """Class attribute that serves a built-in tool's ``description`` from its data file.

    ``description = description_from_file("spawn")`` reads ``descriptions/spawn.txt``
    on first instance access, not at import: a missing file raises
    :class:`DescriptionFileError` naming the tool and path when the description is
    used, and never breaks importing the tool module.
    """

    def __init__(self, name: str) -> None:
        if not _DESCRIPTION_NAME.fullmatch(name):
            raise ValueError(f"description file name must match [a-z][a-z0-9_]*: {name!r}")
        self.file_name = name

    @property
    def path(self) -> Path:
        return DESCRIPTIONS_DIR / f"{self.file_name}.txt"

    @typing.overload
    def __get__(self, obj: None, owner: type | None = None) -> description_from_file: ...

    @typing.overload
    def __get__(self, obj: object, owner: type | None = None) -> str: ...

    def __get__(self, obj: object, owner: type | None = None) -> str | description_from_file:
        # Class access returns the descriptor, as ``property`` does: ABCMeta reads
        # ``getattr(cls, "description")`` while creating the class, and that must not
        # touch the file (a missing file would then break importing the module).
        if obj is None:
            return self
        return builtin_description(self.file_name)


def description_source(cls: type) -> Path | None:
    """The data file a tool class reads its description from; ``None`` for Python text."""
    attr = inspect.getattr_static(cls, "description", None)
    return attr.path if isinstance(attr, description_from_file) else None


def builtin_description_files(root: Path = DESCRIPTIONS_DIR) -> dict[str, str]:
    """``{tool name: relative path}`` for every built-in description file under ``root``.

    The same shape as ``PluginManifest.descriptions``.
    """
    return {p.stem: p.name for p in sorted(root.glob("*.txt"))}


def builtin_description_hashes(root: Path = DESCRIPTIONS_DIR) -> dict[str, str]:
    """Per-tool description hash (``compute_descriptions_hash`` over one file)."""
    from nanobot.kernel.manifest import compute_descriptions_hash  # lazy: import cycle

    return {
        name: compute_descriptions_hash(root, {name: rel})
        for name, rel in builtin_description_files(root).items()
    }


def builtin_descriptions_hash(root: Path = DESCRIPTIONS_DIR) -> str:
    """One hash over every built-in description file.

    It uses the same fold ``compute_version_hash`` applies to a plugin's
    ``descriptions``. That makes it the input a future built-in manifest would carry.
    """
    from nanobot.kernel.manifest import compute_descriptions_hash  # lazy: import cycle

    return compute_descriptions_hash(root, builtin_description_files(root))


# Matches :meth:`Tool._cast_value` / :meth:`Schema.validate_json_schema_value` behavior
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def json_type_name(value: Any) -> str:
    """The JSON type name of a Python value (``bool`` is ``boolean``, not ``integer``)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class SchemaViolation(typing.NamedTuple):
    """One value that fails its JSON Schema (arguments or a typed result, design 5b).

    - ``path``: the field path, ``a.b[0].c``; ``""`` for the value itself.
    - ``detail``: what was expected, ``expected integer, got string``. It names the
      offending value's JSON type, never the value; field names (the path) do appear.
    - ``message``: the legacy text (``a.b should be integer``) kept byte-identical for
      :meth:`Schema.validate_json_schema_value` and the tests that pin it.
    """

    path: str
    detail: str
    message: str

    def describe(self) -> str:
        """``<path>: <detail>``, e.g. ``a.b.c: expected integer, got string``."""
        return f"{self.path or '(value)'}: {self.detail}"


class Schema(ABC):
    """Abstract base for JSON Schema fragments describing tool parameters.

    Concrete types live in :mod:`nanobot.agent.tools.schema`; all implement
    :meth:`to_json_schema` and :meth:`validate_value`. Class methods
    :meth:`validate_json_schema_value` and :meth:`fragment` are the shared validation and normalization entry points.
    """

    @staticmethod
    def resolve_json_schema_type(t: Any) -> str | None:
        """Resolve the non-null type name from JSON Schema ``type`` (e.g. ``['string','null']`` -> ``'string'``)."""
        if isinstance(t, list):
            types = cast(list[Any], t)
            return cast(str | None, next((x for x in types if x != "null"), None))
        return cast(str | None, t)

    @staticmethod
    def subpath(path: str, key: str) -> str:
        return f"{path}.{key}" if path else key

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        """Validate ``val`` against a JSON Schema fragment; returns error messages (empty means valid).

        Used by :class:`Tool` and each concrete Schema's :meth:`validate_value`. The
        messages are :meth:`schema_violations`' ``message`` fields, unchanged.
        """
        return [v.message for v in Schema.schema_violations(val, schema, path)]

    @staticmethod
    def _matches_type(val: Any, t: str, integral_floats: bool) -> bool:
        """True when *val* is of JSON type *t* (an unknown type name matches anything)."""
        if t == "integer":
            if isinstance(val, int) and not isinstance(val, bool):
                return True
            return (
                integral_floats and isinstance(val, float)
                and math.isfinite(val) and val.is_integer()
            )
        if t == "number":
            return isinstance(val, _JSON_TYPE_MAP["number"]) and not isinstance(val, bool)
        if t == "null":
            return val is None
        expected = _JSON_TYPE_MAP.get(t)
        return expected is None or isinstance(val, expected)

    @staticmethod
    def schema_violations(
        val: Any, schema: dict[str, Any], path: str = "", *, integral_floats: bool = False,
    ) -> list[SchemaViolation]:
        """Validate ``val`` against a JSON Schema fragment; one structured record per problem.

        Each :class:`SchemaViolation` carries the field ``path`` (``a.b[0].c``), a
        ``detail`` naming what was expected (``expected integer, got string``) and the
        legacy ``message`` that :meth:`validate_json_schema_value` returns. The subset
        understood: ``type`` (with ``null`` unions / ``nullable``), ``enum``,
        ``minimum``/``maximum``, ``minLength``/``maxLength``, ``properties``,
        ``required``, ``additionalProperties``, ``items``, ``minItems``/``maxItems``.
        Any other keyword is ignored (accepted).

        - A ``type`` list is a union: the value passes when it matches ANY listed type,
          and is then checked against the schema's other keywords as that type. On a
          mismatch the legacy message names the first non-null type, as before.
        - ``integral_floats`` (results only, ``nanobot.kernel.typed``): a finite float with
          no fractional part (``4.0``) satisfies ``integer``, as in JSON Schema. Argument
          validation keeps it off, so a tool never receives ``4.0`` for an integer param.
        """
        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            members = [x for x in raw_type if isinstance(x, str) and x != "null"]
            if len(members) > 1:
                if "null" in raw_type and val is None:
                    return []
                for member in members:
                    if Schema._matches_type(val, member, integral_floats):
                        return Schema.schema_violations(
                            val, {**schema, "type": member}, path,
                            integral_floats=integral_floats,
                        )
                label = path or "parameter"
                return [SchemaViolation(
                    path,
                    f"expected {' or '.join(members)}, got {json_type_name(val)}",
                    f"{label} should be {members[0]}",
                )]
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get("nullable", False)
        t = Schema.resolve_json_schema_type(raw_type)
        label = path or "parameter"

        def bad(detail: str, message: str) -> SchemaViolation:
            return SchemaViolation(path, detail, message)

        def mismatch(expected: str) -> list[SchemaViolation]:
            return [bad(
                f"expected {expected}, got {json_type_name(val)}", f"{label} should be {expected}",
            )]

        if nullable and val is None:
            return []
        if t == "integer" and not Schema._matches_type(val, "integer", integral_floats):
            return mismatch("integer")
        if t == "number" and (
            not isinstance(val, _JSON_TYPE_MAP["number"]) or isinstance(val, bool)
        ):
            return mismatch("number")
        if t in _JSON_TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _JSON_TYPE_MAP[t]):
            return mismatch(t)
        if t == "number" and isinstance(val, float) and not math.isfinite(val):
            return [bad("expected a finite number", f"{label} must be finite")]

        errors: list[SchemaViolation] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(bad(
                f"expected one of {schema['enum']}", f"{label} must be one of {schema['enum']}",
            ))
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(bad(
                    f"expected >= {schema['minimum']}", f"{label} must be >= {schema['minimum']}",
                ))
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(bad(
                    f"expected <= {schema['maximum']}", f"{label} must be <= {schema['maximum']}",
                ))
        if t == "string":
            string_value = cast(str, val)
            if "minLength" in schema and len(string_value) < schema["minLength"]:
                errors.append(bad(
                    f"expected at least {schema['minLength']} chars",
                    f"{label} must be at least {schema['minLength']} chars",
                ))
            if "maxLength" in schema and len(string_value) > schema["maxLength"]:
                errors.append(bad(
                    f"expected at most {schema['maxLength']} chars",
                    f"{label} must be at most {schema['maxLength']} chars",
                ))
        if t == "object":
            object_value = cast(dict[str, Any], val)
            props = cast(dict[str, Any], schema.get("properties", {}))
            required = cast(list[Any], schema.get("required", []))
            for k in required:
                if k not in object_value:
                    sub = Schema.subpath(path, k)
                    errors.append(SchemaViolation(
                        sub, "required field missing", f"missing required {sub}",
                    ))
            additional = schema.get("additionalProperties", True)
            for k, v in object_value.items():
                sub = Schema.subpath(path, k)
                if k in props:
                    errors.extend(Schema.schema_violations(
                        v, props[k], sub, integral_floats=integral_floats,
                    ))
                elif additional is False:
                    errors.append(SchemaViolation(
                        sub, "unexpected field (not in the schema)", f"unexpected parameter {sub}",
                    ))
                elif isinstance(additional, dict):
                    errors.extend(
                        Schema.schema_violations(
                            v, cast(dict[str, Any], additional), sub,
                            integral_floats=integral_floats,
                        )
                    )
        if t == "array":
            array_value = cast(list[Any], val)
            if "minItems" in schema and len(array_value) < schema["minItems"]:
                errors.append(bad(
                    f"expected at least {schema['minItems']} items",
                    f"{label} must have at least {schema['minItems']} items",
                ))
            if "maxItems" in schema and len(array_value) > schema["maxItems"]:
                errors.append(bad(
                    f"expected at most {schema['maxItems']} items",
                    f"{label} must be at most {schema['maxItems']} items",
                ))
            if "items" in schema:
                prefix = f"{path}[{{}}]" if path else "[{}]"
                for i, item in enumerate(array_value):
                    errors.extend(
                        Schema.schema_violations(
                            item, schema["items"], prefix.format(i),
                            integral_floats=integral_floats,
                        )
                    )
        return errors

    @staticmethod
    def fragment(value: Any) -> dict[str, Any]:
        """Normalize a Schema instance or an existing JSON Schema dict to a fragment dict."""
        # Try to_json_schema first: Schema instances must be distinguished from dicts that are already JSON Schema
        to_js = getattr(value, "to_json_schema", None)
        if callable(to_js):
            return cast(dict[str, Any], to_js())
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        raise TypeError(f"Expected schema object or dict, got {type(value).__name__}")

    @abstractmethod
    def to_json_schema(self) -> dict[str, Any]:
        """Return a fragment dict compatible with :meth:`validate_json_schema_value`."""
        ...

    def validate_value(self, value: Any, path: str = "") -> list[str]:
        """Validate a single value; returns error messages (empty means pass). Subclasses may override for extra rules."""
        return Schema.validate_json_schema_value(value, self.to_json_schema(), path)


_NO_STRUCTURED: Any = object()


class ToolResult(str):
    """String-compatible tool output with structured status.

    ``structured`` (optional, Task 21) is the typed payload behind the text: the value a
    tool's ``output_schema`` is checked against (``nanobot.kernel.typed.validate_result``)
    when present, instead of parsing the text. The text is still what the model sees.
    String operations (``result + hint``) return a plain ``str`` and drop it.
    """

    is_error: bool
    structured: Any
    has_structured: bool

    def __new__(
        cls, content: str, *, is_error: bool = False, structured: Any = _NO_STRUCTURED,
    ) -> ToolResult:
        obj = str.__new__(cls, content)
        obj.is_error = is_error
        obj.structured = None if structured is _NO_STRUCTURED else structured
        obj.has_structured = structured is not _NO_STRUCTURED
        return obj

    @classmethod
    def error(cls, content: str) -> ToolResult:
        return cls(content, is_error=True)


class Tool(ABC):
    """Agent capability: read files, run commands, etc."""

    _TYPE_MAP = _JSON_TYPE_MAP
    _BOOL_TRUE = frozenset(("true", "1", "yes"))
    _BOOL_FALSE = frozenset(("false", "0", "no"))

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """Pick first non-null type from JSON Schema unions like ``['string','null']``."""
        return Schema.resolve_json_schema_type(t)

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        ...

    @property
    def read_only(self) -> bool:
        """Whether this tool is side-effect free and safe to parallelize."""
        return False

    @property
    def concurrency_safe(self) -> bool:
        """Whether this tool can run alongside other concurrency-safe tools."""
        return self.read_only and not self.exclusive

    @property
    def exclusive(self) -> bool:
        """Whether this tool should run alone even if concurrency is enabled."""
        return False

    # --- Plugin metadata ---

    config_key: str = ""
    _plugin_discoverable: bool = True
    _scopes: set[str] = {"core"}
    # Every capability name ``capabilities`` can return, for any params: the static
    # surface a sub-agent requests when its policy is attenuated (I4). Declared on the
    # same class that overrides ``capabilities``; ``None`` means undeclared.
    _capability_names: frozenset[str] | None = None
    # Typed calls (design 5b, Task 21): a JSON Schema the RESULT must match, checked by
    # ``nanobot.kernel.typed.validate_result`` after ``execute`` on every call path.
    # ``None`` (the default, every built-in) means no check: the result passes exactly
    # as returned. A tool that opts in returns JSON text, a JSON value, or a
    # ``ToolResult`` with ``structured``; anything else fails with
    # ``RESULT_SCHEMA_MARKER``. Class- or instance-level, like ``_capability_names``.
    # By design, text is JSON: ``{"type": "string"}`` needs JSON-quoted text, and plain
    # prose fails. An empty schema (``{}``) accepts anything, prose included.
    output_schema: dict[str, Any] | None = None

    @classmethod
    def config_cls(cls) -> type[BaseModel] | None:
        return None

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def runtime_context_provider(self) -> RuntimeContextProvider | None:
        """Return optional per-turn prompt context owned by this tool."""
        return None

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Run the tool; return content, or ``ToolResult.error(...)`` for failures."""
        ...

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        """Capabilities a call with *params* needs (checked by the kernel gate).

        - Default: none. Built-in tools that touch the filesystem, network, processes,
          sessions or budgets override this.
        - Must not raise on malformed *params* (the gate fails closed and denies the
          call if it does).
        - Resources are the RAW params: ``fs.read``/``fs.write`` paths may be relative or
          padded. The gate (``nanobot.kernel.gate._normalize_fs_resource``) strips them
          and resolves relative paths against the bound workspace scope, else the tool's
          own ``_workspace``/``workspace``, else the host's work dir, before any floor or
          policy sees them. Declare the path the tool resolves, not a pre-resolved one.
        - Existing inline guards inside ``execute`` stay; the gate is additive.
        - Declare every name this can return in ``_capability_names`` on the same class
          (see ``capability_surface``).
        """
        return []

    def capability_surface(self) -> frozenset[str] | None:
        """Every capability name ``capabilities`` can return, or ``None`` if unknown.

        - A tool that does not override ``capabilities`` declares nothing: empty set.
        - Otherwise ``_capability_names`` counts only when it is set on the class that
          overrides ``capabilities`` or a subclass of it; a subclass that overrides
          ``capabilities`` again without redeclaring is ``None`` (a stale inherited
          declaration is never trusted).
        """
        mro = type(self).__mro__
        caps_owner = next(i for i, c in enumerate(mro) if "capabilities" in c.__dict__)
        if mro[caps_owner] is Tool:
            return frozenset()
        names_owner = next(
            (i for i, c in enumerate(mro) if "_capability_names" in c.__dict__), len(mro),
        )
        if names_owner > caps_owner:
            return None
        names = self._capability_names
        return None if names is None else frozenset(names)

    @staticmethod
    def error(content: str) -> ToolResult:
        return ToolResult.error(content)

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return obj
        props = cast(dict[str, Any], schema.get("properties", {}))
        additional = schema.get("additionalProperties")
        casted: dict[str, Any] = {}
        object_value = cast(dict[str, Any], obj)
        for k, v in object_value.items():
            if k in props:
                casted[k] = self._cast_value(v, props[k])
            elif isinstance(additional, dict):
                casted[k] = self._cast_value(v, cast(dict[str, Any], additional))
            else:
                casted[k] = v
        return casted

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply safe schema-driven casts before validation."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params
        return self._cast_object(params, schema)

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        t = self._resolve_type(schema.get("type"))

        if t == "boolean" and isinstance(val, bool):
            return val
        if t == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if t in self._TYPE_MAP and t not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[t]
            if isinstance(val, expected):
                return val

        if isinstance(val, str) and t in ("integer", "number"):
            try:
                return int(val) if t == "integer" else float(val)
            except ValueError:
                return val

        if t == "string":
            return val if val is None else str(val)

        if t == "boolean" and isinstance(val, str):
            low = val.lower()
            if low in self._BOOL_TRUE:
                return True
            if low in self._BOOL_FALSE:
                return False
            return val

        if t == "array" and isinstance(val, list):
            items = schema.get("items")
            array_value = cast(list[Any], val)
            return [self._cast_value(x, items) for x in array_value] if items else array_value

        if t == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate against JSON schema; empty list means valid."""
        if not isinstance(cast(object, params), dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return Schema.validate_json_schema_value(params, {**schema, "type": "object"}, "")

    def parameter_violations(self, params: dict[str, Any]) -> list[SchemaViolation]:
        """Structured form of :meth:`validate_params`' schema check (path + expected type).

        Used for the ``Fields to fix`` line of an invalid-parameters error; the legacy
        ``validate_params`` strings are unchanged.
        """
        if not isinstance(cast(object, params), dict):
            return [SchemaViolation(
                "", f"expected object, got {json_type_name(params)}",
                f"parameters must be an object, got {type(params).__name__}",
            )]
        schema = self.parameters or {}
        return Schema.schema_violations(params, {**schema, "type": "object"}, "")

    def to_schema(self) -> dict[str, Any]:
        """OpenAI function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_parameters(schema: dict[str, Any]) -> Callable[[type[_ToolT]], type[_ToolT]]:
    """Class decorator: attach JSON Schema and inject a concrete ``parameters`` property.

    Use on ``Tool`` subclasses instead of writing ``@property def parameters``. The
    schema is stored on the class and returned as a fresh copy on each access.

    Example::

        @tool_parameters({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        })
        class ReadFileTool(Tool):
            ...
    """

    def decorator(cls: type[_ToolT]) -> type[_ToolT]:
        frozen = deepcopy(schema)

        @property
        def parameters(self: Any) -> dict[str, Any]:
            return deepcopy(frozen)

        cls.parameters = parameters  # type: ignore[assignment]

        abstract = getattr(cls, "__abstractmethods__", None)
        if abstract is not None and "parameters" in abstract:
            cls.__abstractmethods__ = frozenset(abstract - {"parameters"})  # type: ignore[misc]

        return cls

    return decorator
