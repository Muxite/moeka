"""Tool discovery and registration via package scanning.

Two plugin modes (built-in tools load the same way in both):

- Legacy (default, ``ToolLoader()``): entry-point plugins (group ``nanobot.tools``) are
  imported at discovery and registered like built-ins; a plugin whose name a built-in
  already holds is skipped with a warning. No manifest, registry or config check.
- Kernel mode (``ToolLoader(plugin_registry=...)``, design section 9 / P4): an
  entry-point plugin loads only when all of these hold, checked in this order:

  1. its top-level package directory has a ``moeka-plugin.json`` (kind ``tool``) whose
     ``entry`` is this entry point's ``module:attr``. No manifest means the plugin is
     rejected outright (there is no name or hash to look up);
  2. the host's ``PluginRegistry.check_active`` passes for the hash the LOADER computes
     (``compute_version_hash``) from that package on disk now. The manifest's own
     ``version_hash`` claim is never used. A mismatch auto-quarantines an active entry.
     Only after this is any plugin code imported (bytecode writing is off during the
     import, so loading never changes the package's own hash);
  3. its ``config_cls()``, when not ``None``, validates ``env.config.section(<manifest
     name>)``; the instance reaches ``create`` as ``ToolContext.plugin_config``;
  4. its tool name is not already registered (a built-in or another plugin): a hard
     :class:`LoadError`, since the host activated a plugin that cannot load as approved.

  A manifest ``operations`` entry named like the registered tool binds to it (Task 21,
  typed calls): its ``output_schema`` becomes the tool's ``output_schema`` (checked on
  every result by ``nanobot.kernel.typed.validate_result``) and its ``input_schema`` is
  checked on top of the tool's own ``parameters``. The model still sees the tool's own
  ``parameters``. An operation that names no registered tool binds to nothing.

  A manifest ``descriptions`` entry for the registered tool's name replaces the tool's
  own ``description`` (Task 20, descriptions as data). The file is read before plugin
  code is imported, and its bytes are part of the hash step 2 checked. A file that is
  not UTF-8 rejects the plugin.

Variants (Task 8): when ``ToolContext.variant`` is set, each tool the loader builds gets
``variant.description_for(tool.name)`` as its per-instance description override
(``Tool.set_description_override``); for a kernel plugin it also wins over the
manifest's description. Nothing class-level or process-wide is changed.

  Steps 1-3 failing skip the plugin with a logged error and a ``plugin.load`` deny
  trace event. The registered tool carries ``capability_grant`` = the manifest's
  ``capabilities_requested`` minus what the ToolRegistry's gate policy denies
  everywhere (``policy ∩ requested``); the gate denies any declared request outside it.

Kernel mode is independent of ``CoreEnvironment.strict``: strict is about the exec
sandbox and dropping fully denied tools, while plugin gating is the host's choice of
which third-party code may run at all. Nothing in the loader acts as a host principal.
"""

# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import DescriptionFileError, Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nanobot.agent.tools.base import SchemaViolation
    from nanobot.agent.tools.context import RequestContext, ToolContext
    from nanobot.kernel.manifest import Operation, PluginManifest
    from nanobot.kernel.policy import CapabilityRequest, PermissionPolicy
    from nanobot.kernel.registry import PluginRegistry

_SKIP_MODULES = frozenset({
    "base", "schema", "registry", "context", "loader", "config",
    "file_state", "sandbox", "mcp", "__init__", "runtime_control",
})


class LoadError(Exception):
    """Load failure that aborts ``ToolLoader.load``: a kernel-mode plugin name collision,
    or an unreadable variant description file."""


class _PluginRejectedError(Exception):
    """One kernel-mode plugin is not loaded; the message is the logged reason."""


class ToolLoader:
    def __init__(
        self,
        package: Any = None,
        *,
        test_classes: list[type[Tool]] | None = None,
        plugin_registry: PluginRegistry | None = None,
    ):
        if package is None:
            import nanobot.agent.tools as _pkg
            package = _pkg
        self._package = package
        self._test_classes = test_classes
        # Kernel mode when set: entry-point plugins load only if registry-active.
        self._plugin_registry = plugin_registry
        self._discovered: list[type[Tool]] | None = None
        self._plugins: dict[str, type[Tool]] | None = None

    def discover(self) -> list[type[Tool]]:
        if self._test_classes is not None:
            return list(self._test_classes)
        if self._discovered is not None:
            return self._discovered
        seen: set[int] = set()
        results: list[type[Tool]] = []
        for _importer, module_name, _ispkg in pkgutil.iter_modules(self._package.__path__):
            if module_name.startswith("_") or module_name in _SKIP_MODULES:
                continue
            try:
                module = importlib.import_module(f".{module_name}", self._package.__name__)
            except Exception:
                logger.exception("Failed to import tool module: %s", module_name)
                continue
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Tool)
                    and attr is not Tool
                    and not attr_name.startswith("_")
                    and not getattr(attr, "__abstractmethods__", None)
                    and getattr(attr, "_plugin_discoverable", True)
                    and id(attr) not in seen
                ):
                    seen.add(id(attr))
                    results.append(attr)
        results.sort(key=lambda cls: cls.__name__)
        self._discovered = results
        return results

    def _discover_plugins(self) -> dict[str, type[Tool]]:
        """Discover external tool plugins registered via entry_points."""
        if self._plugins is not None:
            return self._plugins
        plugins: dict[str, type[Tool]] = {}
        try:
            eps = entry_points(group="nanobot.tools")
        except Exception:
            return plugins
        for ep in eps:
            try:
                cls = ep.load()
                if (
                    isinstance(cls, type)
                    and issubclass(cls, Tool)
                    and not getattr(cls, "__abstractmethods__", None)
                    and getattr(cls, "_plugin_discoverable", True)
                ):
                    plugins[ep.name] = cls
            except Exception:
                logger.exception("Failed to load tool plugin: %s", ep.name)
        self._plugins = plugins
        return plugins

    def load(
        self,
        ctx: ToolContext,
        registry: ToolRegistry,
        *,
        scope: str = "core",
        allow: list[str] | None = None,
        deny: list[str] | None = None,
    ) -> list[str]:
        """Discover and register tools, honoring an optional allow/deny scope.

        ``allow=None`` registers every discovered tool (historical behavior);
        an explicit list registers only those names, so tools added to the
        codebase later never silently appear in a scoped agent. ``deny``
        always wins over ``allow``.
        """
        allow_set = set(allow) if allow is not None else None
        deny_set = set(deny or ())
        registered: list[str] = []
        builtin_names: set[str] = set()
        sources: list[tuple[Any, bool]] = [(self.discover(), False)]
        if self._plugin_registry is None:
            sources.append((self._discover_plugins().values(), True))
        for source, is_plugin_source in sources:
            for tool_cls in source:
                cls_label = tool_cls.__name__
                try:
                    if scope not in getattr(tool_cls, "_scopes", {"core"}):
                        continue
                    if not tool_cls.enabled(ctx):
                        continue
                    tool = tool_cls.create(ctx)
                    if tool.name in deny_set or (
                        allow_set is not None and tool.name not in allow_set
                    ):
                        continue
                    _apply_variant(ctx, tool)
                    if is_plugin_source:
                        tool = _LegacyErrorPrefixTool(tool)
                    if registry.has(tool.name):
                        if is_plugin_source and tool.name in builtin_names:
                            logger.warning(
                                "Plugin %s skipped: conflicts with built-in tool %s",
                                cls_label, tool.name,
                            )
                            continue
                        logger.warning(
                            "Tool name collision: %s from %s overwrites existing",
                            tool.name, cls_label,
                        )
                    if registry.register(tool) is False:
                        continue  # strict mode dropped it (fully denied by policy)
                    registered.append(tool.name)
                    if not is_plugin_source:
                        builtin_names.add(tool.name)
                except LoadError:
                    raise  # a broken variant file fails the load, never drops a tool
                except Exception:
                    logger.exception("Failed to register tool: %s", cls_label)
        if self._plugin_registry is not None:
            registered.extend(
                self._load_kernel_plugins(ctx, registry, scope, allow_set, deny_set)
            )
        return registered

    # -- kernel mode -----------------------------------------------------------

    def _load_kernel_plugins(
        self,
        ctx: ToolContext,
        registry: ToolRegistry,
        scope: str,
        allow_set: set[str] | None,
        deny_set: set[str],
    ) -> list[str]:
        """Register registry-active entry-point plugins (see the module docstring)."""
        try:
            eps = list(entry_points(group="nanobot.tools"))
        except Exception:
            return []
        registered: list[str] = []
        for ep in eps:
            label = str(getattr(ep, "name", ep))
            manifest: PluginManifest | None = None
            try:
                manifest, tool_cls, descriptions = self._admit_plugin(ep)
                if scope not in getattr(tool_cls, "_scopes", {"core"}):
                    continue
                plugin_ctx = _plugin_context(manifest, tool_cls, ctx)
                if not tool_cls.enabled(plugin_ctx):
                    continue
                tool = tool_cls.create(plugin_ctx)
                name = tool.name
            except _PluginRejectedError as exc:
                _reject(ctx, label, manifest, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one broken plugin never stops loading
                logger.exception("Failed to load kernel plugin: {}", label)
                _reject(ctx, label, manifest, f"load failed: {type(exc).__name__}")
                continue
            if name in deny_set or (allow_set is not None and name not in allow_set):
                continue
            if registry.has(name):
                raise LoadError(
                    f"kernel plugin {manifest.name!r} (entry point {label!r}) registers tool "
                    f"{name!r}, which is already registered; a plugin never shadows a "
                    "built-in or another plugin"
                )
            grant = _capability_grant(manifest, registry.gate_policy)
            operation = next((op for op in manifest.operations if op.name == name), None)
            variant_text = _apply_variant(ctx, tool)
            wrapped = _KernelPluginTool(
                tool, plugin=manifest.name, grant=grant,
                description=variant_text if variant_text is not None else descriptions.get(name),
                operation=operation,
            )
            if registry.register(wrapped):
                registered.append(name)
                _emit(ctx, {
                    "event": "plugin.load", "verdict": "allow", "plugin": manifest.name,
                    "entry_point": label, "tool": name, "grant": list(grant), "reason": "",
                })
        return registered

    def _admit_plugin(
        self, ep: Any,
    ) -> tuple[PluginManifest, type[Tool], dict[str, str]]:
        """Manifest + registry check on the bytes on disk, THEN import the plugin.

        Returns the manifest, the tool class and the manifest's description texts
        (``{tool name: text}``). Raises :class:`_PluginRejectedError` before any plugin
        code runs when the package has no valid manifest for this entry point, is not
        active at its current hash, or has an unreadable description file.
        """
        from nanobot.kernel.manifest import (  # lazy: Ruling C
            MANIFEST_FILENAME,
            PluginManifest,
            compute_version_hash,
        )

        registry = self._plugin_registry
        assert registry is not None
        root = _plugin_root(ep)
        if root is None:
            raise _PluginRejectedError("no plugin package directory, so no moeka-plugin.json manifest")
        manifest_path = root / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise _PluginRejectedError(f"no {MANIFEST_FILENAME} manifest in {root}")
        try:
            manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - an unreadable manifest is a rejection
            raise _PluginRejectedError(f"invalid {MANIFEST_FILENAME} manifest: {type(exc).__name__}")
        if manifest.kind != "tool":
            raise _PluginRejectedError(f"manifest kind is {manifest.kind!r}, not 'tool'")
        entry = str(getattr(ep, "value", ""))
        if manifest.entry != entry:
            raise _PluginRejectedError(
                f"manifest entry {manifest.entry!r} does not match the entry point {entry!r}"
            )
        # The hash of the package as it will be imported, computed here from disk; a
        # package that cannot be hashed never matches the pin (and is quarantined).
        try:
            current = compute_version_hash(root, manifest)
        except ValueError as exc:
            logger.warning("kernel plugin {} package unhashable: {}", manifest.name, exc)
            current = "unhashable"
        if not registry.check_active(manifest.name, current):
            try:
                record = registry.get(manifest.name)
                state = record.state if record is not None else "not registered"
            except Exception:  # noqa: BLE001 - a corrupt registry already failed closed
                state = "registry unreadable"
            raise _PluginRejectedError(
                f"plugin {manifest.name!r} is not active at its on-disk hash ({state})"
            )
        descriptions = _read_descriptions(root, manifest)
        with _no_bytecode():
            cls = ep.load()
        if not (
            isinstance(cls, type)
            and issubclass(cls, Tool)
            and not getattr(cls, "__abstractmethods__", None)
            and getattr(cls, "_plugin_discoverable", True)
        ):
            raise _PluginRejectedError(f"entry {entry!r} is not a concrete, discoverable Tool")
        return manifest, cls, descriptions


def _read_descriptions(root: Path, manifest: PluginManifest) -> dict[str, str]:
    """``{tool name: text}`` from the manifest's description files (Task 20).

    Read right after the hash check, with the same UTF-8 / one-trailing-newline rule as
    built-in description files. A file that cannot be read or decoded rejects the plugin.
    """
    from nanobot.agent.tools.base import DescriptionFileError, read_description_file

    real_root = root.resolve()
    texts: dict[str, str] = {}
    for tool, rel in (manifest.descriptions or {}).items():
        target = (root / rel).resolve()
        if not target.is_relative_to(real_root):  # compute_version_hash rejects this too
            raise _PluginRejectedError(f"description file for {tool!r} escapes the package")
        try:
            texts[tool] = read_description_file(target, tool=tool)
        except DescriptionFileError as exc:
            raise _PluginRejectedError(f"description file for {tool!r} rejected: {exc}")
    return texts


def _plugin_root(ep: Any) -> Path | None:
    """Directory of the entry point's top-level package, found WITHOUT importing it."""
    try:
        top = str(ep.module).split(".", 1)[0]
        spec = importlib.util.find_spec(top)
    except Exception:  # noqa: BLE001 - no locatable package means no manifest
        return None
    if spec is None or spec.origin is None:
        return None  # not found, or a namespace package (no single root)
    locations = list(spec.submodule_search_locations or ())
    if len(locations) != 1:
        return None  # a single-file module has no package directory of its own
    return Path(locations[0])


@contextmanager
def _no_bytecode() -> Iterator[None]:
    """Import without writing ``__pycache__`` into the hash-pinned package.

    Process-global flag; tool loading happens once at startup on one thread.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def _plugin_context(manifest: PluginManifest, tool_cls: type[Tool], ctx: ToolContext) -> ToolContext:
    """Validate the plugin's config section with ``config_cls()`` (skipped when ``None``)."""
    try:
        model = tool_cls.config_cls()
    except Exception as exc:  # noqa: BLE001
        raise _PluginRejectedError(f"config_cls() raised {type(exc).__name__}") from None
    if model is None:
        return ctx
    validate = getattr(model, "model_validate", None)
    if not callable(validate):
        raise _PluginRejectedError("config_cls() is not a pydantic model")
    try:
        section = ctx.env.config.section(manifest.name) if ctx.env is not None else {}
    except Exception as exc:  # noqa: BLE001
        raise _PluginRejectedError(
            f"config section {manifest.name!r} unreadable: {type(exc).__name__}"
        ) from None
    try:
        config = validate(section)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError or model error
        raise _PluginRejectedError(
            f"config section {manifest.name!r} invalid: {_config_errors(exc)}"
        ) from None
    return dataclasses.replace(ctx, plugin_config=config)


def _config_errors(exc: Exception) -> str:
    """Field locations and messages only: never the rejected input values."""
    errors = getattr(exc, "errors", None)
    try:
        items = errors(include_input=False) if callable(errors) else None
    except Exception:  # noqa: BLE001
        items = None
    if not items:
        return type(exc).__name__
    return "; ".join(
        f"{'.'.join(str(p) for p in item.get('loc', ())) or '<root>'}: {item.get('msg', '')}"
        for item in items
    )


def _capability_grant(
    manifest: PluginManifest, policy: PermissionPolicy | None,
) -> tuple[str, ...]:
    """``policy ∩ capabilities_requested``: the requested rules the policy does not deny
    everywhere. The per-resource policy check still runs at call time in the gate."""
    from nanobot.kernel.policy import DefaultPolicy, policy_denies_everywhere

    effective = policy if policy is not None else DefaultPolicy()
    return tuple(
        rule for rule in manifest.capabilities_requested
        if not policy_denies_everywhere(effective, rule.split(":", 1)[0])
    )


def _apply_variant(ctx: ToolContext, tool: Tool) -> str | None:
    """Set the variant's description override on *tool* (this instance); return it."""
    variant = getattr(ctx, "variant", None)
    if variant is None:
        return None
    try:
        text = variant.description_for(tool.name)
    except DescriptionFileError as exc:
        raise LoadError(f"variant {variant.name!r}: {exc}") from exc
    if text is not None:
        tool.set_description_override(text)
    return text


def _emit(ctx: ToolContext, event: dict[str, Any]) -> None:
    if ctx.env is None:
        return
    from nanobot.kernel.trace import safe_emit

    safe_emit(ctx.env.trace, event)


def _reject(ctx: ToolContext, label: str, manifest: PluginManifest | None, reason: str) -> None:
    logger.error("Kernel plugin {} not loaded: {}", label, reason)
    _emit(ctx, {
        "event": "plugin.load", "verdict": "deny",
        "plugin": manifest.name if manifest is not None else None,
        "entry_point": label, "tool": None, "grant": None, "reason": reason,
    })


class _LegacyErrorPrefixTool(Tool):
    """Compatibility wrapper for external tools using the old error-string contract."""

    _plugin_discoverable = False

    def __init__(self, wrapped: Tool) -> None:
        self._wrapped = wrapped

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def description(self) -> str:
        return self._wrapped.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._wrapped.parameters

    def runtime_context_provider(self):
        return self._wrapped.runtime_context_provider()

    @property
    def read_only(self) -> bool:
        return self._wrapped.read_only

    @property
    def exclusive(self) -> bool:
        return self._wrapped.exclusive

    @property
    def concurrency_safe(self) -> bool:
        return self._wrapped.concurrency_safe

    @property
    def config_key(self) -> str:
        return getattr(self._wrapped, "config_key", "")

    def set_context(self, ctx: RequestContext) -> None:
        set_context = getattr(self._wrapped, "set_context", None)
        if callable(set_context):
            set_context(ctx)

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._wrapped.cast_params(params)

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return self._wrapped.capabilities(params)

    def capability_surface(self) -> frozenset[str] | None:
        return self._wrapped.capability_surface()

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return self._wrapped.validate_params(params)

    def parameter_violations(self, params: dict[str, Any]) -> list[SchemaViolation]:
        return self._wrapped.parameter_violations(params)

    @property
    def output_schema(self) -> dict[str, Any] | None:  # type: ignore[override]
        # ``Tool.output_schema`` is a class attribute, so ``__getattr__`` never sees it.
        return getattr(self._wrapped, "output_schema", None)

    def to_schema(self) -> dict[str, Any]:
        return self._wrapped.to_schema()

    async def execute(self, **kwargs: Any) -> Any:
        result = await self._wrapped.execute(**kwargs)
        if (
            isinstance(result, str)
            and not isinstance(result, ToolResult)
            and result.startswith("Error:")
        ):
            return ToolResult.error(result)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class _KernelPluginTool(_LegacyErrorPrefixTool):
    """A kernel-mode plugin tool with its load-time grant (``policy ∩ requested``).

    ``capability_grant`` is read by the gate (``nanobot.kernel.gate``): a declared
    request outside it is denied.

    ``capability_surface`` is deliberately NOT narrowed to the grant: it stays the
    wrapped tool's true static surface, as for every other tool. Strict mode's drop
    (``ToolRegistry._strict_drop`` -> ``fully_denied``) must see the capabilities the
    tool can ask for; a narrowed surface is empty exactly when the policy denies all of
    them everywhere, and ``fully_denied`` keeps an empty surface, so narrowing hid such a
    plugin from the drop. Sub-agent attenuation may therefore see a wider set than the
    grant, which widens nothing: the gate checks the grant on every call.
    """

    _plugin_discoverable = False

    def __init__(
        self, wrapped: Tool, *, plugin: str, grant: tuple[str, ...],
        description: str | None = None, operation: Operation | None = None,
    ) -> None:
        super().__init__(wrapped)
        self._plugin = plugin
        self._grant = tuple(grant)
        # The manifest's description file text, when it names this tool (Task 20).
        self._description = description
        # The manifest operation named like this tool (Task 21, typed calls): its
        # schemas are the host-approved, hash-pinned contract.
        self._operation = operation

    @property
    def operation(self) -> Operation | None:
        return self._operation

    @property
    def output_schema(self) -> dict[str, Any] | None:  # type: ignore[override]
        """The operation's ``output_schema`` when one is bound, else the tool's own."""
        if self._operation is not None:
            return self._operation.output_schema
        return super().output_schema

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """The tool's own check, plus the bound operation's ``input_schema``."""
        errors = list(self._wrapped.validate_params(params))
        if self._operation is not None and isinstance(params, dict):
            from nanobot.agent.tools.base import Schema

            errors.extend(
                e for e in Schema.validate_json_schema_value(params, self._operation.input_schema)
                if e not in errors
            )
        return errors

    def parameter_violations(self, params: dict[str, Any]) -> list[SchemaViolation]:
        violations = list(self._wrapped.parameter_violations(params))
        if self._operation is not None and isinstance(params, dict):
            from nanobot.agent.tools.base import Schema

            seen = {v.message for v in violations}
            violations.extend(
                v for v in Schema.schema_violations(params, self._operation.input_schema)
                if v.message not in seen
            )
        return violations

    @property
    def description(self) -> str:
        if self._description is not None:
            return self._description
        return self._wrapped.description

    def to_schema(self) -> dict[str, Any]:
        schema = self._wrapped.to_schema()
        if self._description is None:
            return schema
        function = dict(schema.get("function") or {})
        function["description"] = self._description
        return {**schema, "function": function}

    @property
    def plugin_name(self) -> str:
        return self._plugin

    @property
    def capability_grant(self) -> tuple[str, ...]:
        return self._grant
