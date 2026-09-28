"""Tool registry for dynamic tool management."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ContextAware, current_request_context

if TYPE_CHECKING:
    from pathlib import Path

    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.gate import GateResult
    from nanobot.kernel.policy import PermissionPolicy, Principal
    from nanobot.runtime_context import RuntimeContextProvider


def is_tool_error_result(result: Any) -> bool:
    return isinstance(result, ToolResult) and result.is_error


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        # Capability gate settings for ``execute`` (kernel P2). Unset means
        # ``DefaultPolicy()``, the top-level agent and the legacy floor roots.
        self.gate_policy: PermissionPolicy | None = None
        self.gate_principal: Principal | None = None
        self.gate_env: CoreEnvironment | None = None
        self.gate_workspace: Path | None = None
        # Optional owner-set admission check (a kernel agent's tool scope): a tool it
        # rejects is never registered, whoever registers it (loader, MCP, host).
        self.admit: Callable[[Tool], bool] | None = None

    def configure_gate(
        self,
        *,
        policy: PermissionPolicy | None = None,
        principal: Principal | None = None,
        env: CoreEnvironment | None = None,
        workspace: Path | None = None,
    ) -> None:
        """Set the policy/principal/env that ``execute`` gates every call with.

        In strict mode with an explicit *policy*, tools already registered whose whole
        capability surface the policy denies everywhere are pruned (see ``register``).
        """
        self.gate_policy = policy
        self.gate_principal = principal
        self.gate_env = env
        self.gate_workspace = workspace
        for name, tool in list(self._tools.items()):
            if self._strict_drop(tool):
                self._tools.pop(name, None)
                self._cached_definitions = None

    def gate(self, tool: Tool, params: Any) -> GateResult:
        """Run the kernel capability gate for one prepared call."""
        from nanobot.kernel.gate import gate_call

        return gate_call(
            tool,
            params,
            self.gate_principal,
            self.gate_policy,
            self.gate_env,
            workspace=self.gate_workspace,
        )

    def emit_invalid(self, name: str, error: str) -> None:
        """Record a call that failed preparation as a ``tool.invalid`` trace event."""
        from nanobot.kernel.gate import emit_tool_invalid

        emit_tool_invalid(self.gate_env, name, self.gate_principal, error)

    def _strict_drop(self, tool: Tool) -> bool:
        """Strict mode: True when the gate policy denies *tool*'s whole surface everywhere.

        - Applies only with a strict ``gate_env`` AND an explicit ``gate_policy``; the
          permissive default (no policy) never drops anything.
        - An empty or unknown (``None``) surface is kept: the gate decides per call.
        - A drop is logged and emitted as a ``tool.dropped`` trace event.
        """
        from nanobot.kernel.strict import fully_denied, is_strict

        if self.gate_policy is None or not is_strict(self.gate_env):
            return False
        surface_fn = getattr(tool, "capability_surface", None)
        try:
            surface = surface_fn() if callable(surface_fn) else None
        except Exception:  # noqa: BLE001 - an unknown surface keeps the tool
            surface = None
        if not fully_denied(self.gate_policy, surface):
            return False
        from loguru import logger

        from nanobot.kernel.trace import safe_emit

        name = tool.name
        logger.info("strict mode: tool {} dropped, every capability is denied", name)
        assert self.gate_env is not None  # guarded by is_strict()
        safe_emit(self.gate_env.trace, {
            "event": "tool.dropped",
            "tool": name,
            "capabilities": sorted(surface or ()),
            "reason": "strict mode: every capability denied for every resource",
        })
        return True

    def register(self, tool: Tool) -> bool:
        """Register a tool (a tool with ``bind_registry`` is handed this registry).

        Returns False when strict mode drops it (``_strict_drop``): the tool is then not
        registered, any earlier tool of the same name is removed, and the model never
        sees it. Also returns False, registering nothing and leaving any earlier tool
        in place, when ``admit`` rejects it.
        """
        if self.admit is not None and not self.admit(tool):
            from loguru import logger

            logger.debug("tool {} not admitted by this registry's scope", tool.name)
            return False
        if self._strict_drop(tool):
            self.unregister(tool.name)
            return False
        self._tools[tool.name] = tool
        self._cached_definitions = None
        binder = getattr(tool, "bind_registry", None)
        if callable(binder):
            binder(self)
        return True

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_runtime_context_providers(self) -> list[RuntimeContextProvider]:
        """Return tool-owned providers in stable tool-name order."""
        providers: list[RuntimeContextProvider] = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self.get(name) is not None

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = cast(dict[str, Any], fn).get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended. The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is None:
            definitions = [tool.to_schema() for tool in self._tools.values()]
            builtins: list[dict[str, Any]] = []
            mcp_tools: list[dict[str, Any]] = []
            for schema in definitions:
                name = self._schema_name(schema)
                if name.startswith("mcp_"):
                    mcp_tools.append(schema)
                else:
                    builtins.append(schema)

            builtins.sort(key=self._schema_name)
            mcp_tools.sort(key=self._schema_name)
            self._cached_definitions = builtins + mcp_tools

        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )
        # Compatibility for external tools that still implement the legacy
        # setter protocol. Built-ins read the authoritative ContextVar
        # directly and never copy routing state.
        if isinstance(tool, ContextAware) and (ctx := current_request_context()) is not None:
            tool.set_context(ctx)

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        cast_params = tool.cast_params(cast(dict[str, Any], params))
        errors = tool.validate_params(cast_params)
        if errors:
            text = f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            fixes = self._field_fixes(tool, cast_params)
            if fixes:
                text += "\nFields to fix: " + "; ".join(fixes)
            return tool, cast_params, ToolResult.error(text)
        return tool, cast_params, None

    @staticmethod
    def _field_fixes(tool: Tool, params: dict[str, Any]) -> list[str]:
        """Each bad argument as ``<path>: <expected>`` (design 5b: repair in one step).

        From ``Tool.parameter_violations`` (the same schema ``validate_params`` checks).
        The legacy messages stay first and unchanged; this is an extra line. A tool whose
        own ``validate_params`` adds rules beyond its schema only gets the schema's part.
        """
        try:
            violations = tool.parameter_violations(params)
        except Exception:  # noqa: BLE001 - the legacy error text is still returned
            return []
        return [v.describe() for v in violations]

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        arguments_payload = cast(dict[str, Any], params)
        if set(arguments_payload) != {"arguments"}:
            return arguments_payload
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return arguments_payload
        return cls._coerce_argument_value(arguments_payload.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """Execute a tool by name with given parameters."""
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            self.emit_invalid(name, str(error))
            return ToolResult.error(str(error) + hint)
        assert tool is not None  # guarded by prepare_call()
        gate = self.gate(tool, params)
        if not gate.allowed:
            return ToolResult.error(gate.error_text() + hint)

        try:
            result = await tool.execute(**params)
            # Typed calls (design 5b): check the result against the tool's output_schema.
            from nanobot.kernel.gate import validate_result

            result = validate_result(
                tool, result, env=self.gate_env, principal=self.gate_principal,
            )
            if is_tool_error_result(result):
                return ToolResult.error(str(result) + hint)
            return result
        except Exception as e:
            return ToolResult.error(f"Error executing {name}: {str(e)}" + hint)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
