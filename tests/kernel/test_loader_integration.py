"""Kernel-mode plugin loading (Task 19, design section 9 / P4).

``ToolLoader(plugin_registry=...)`` is kernel mode. There, an entry-point plugin loads
only when:
- its package has a ``moeka-plugin.json`` naming this entry point;
- the host's ``PluginRegistry`` has it ``active`` at the hash the LOADER computes from
  the package on disk (a tampered package is auto-quarantined, never imported);
- its ``config_cls()`` (when not ``None``) validates ``env.config.section(name)``;
- its tool name shadows nothing already registered (a hard ``LoadError``).

Its capability grant is ``policy ∩ capabilities_requested`` and the gate enforces it.
Built-in tools and the default (legacy) loader are unchanged.
"""

from __future__ import annotations

import json
import sys
import uuid
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.loader import LoadError, ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.manifest import MANIFEST_FILENAME, PluginManifest, compute_version_hash
from nanobot.kernel.policy import DefaultPolicy, Principal
from nanobot.kernel.registry import PluginRegistry

HOST = Principal("host", "host")

_TOOL_SRC = '''
from typing import Any

from pydantic import BaseModel

from nanobot.agent.tools.base import Tool
from nanobot.kernel.policy import CapabilityRequest

IMPORTED = True


class Cfg(BaseModel):
    limit: int


class PluginTool(Tool):
    _capability_names = frozenset({{"net.fetch", "fs.write"}})

    @property
    def name(self) -> str:
        return {tool_name!r}

    @property
    def description(self) -> str:
        return "kernel test plugin"

    @property
    def parameters(self) -> dict[str, Any]:
        return {{"type": "object", "properties": {{
            "cap": {{"type": "string"}}, "resource": {{"type": "string"}},
        }}}}

    @classmethod
    def config_cls(cls):
        return {config_cls}

    @classmethod
    def create(cls, ctx):
        tool = cls()
        tool.seen_config = ctx.plugin_config
        return tool

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        cap = params.get("cap")
        return [CapabilityRequest(cap, params.get("resource", ""))] if cap else []

    async def execute(self, **kwargs: Any) -> Any:
        return "plugin ran"
'''


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _Config:
    def __init__(self, sections: dict[str, dict[str, Any]] | None = None) -> None:
        self.sections = sections or {}

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.sections.get(name, {}))


class _Plugin:
    """A real on-disk tool plugin package importable from ``sys.path``."""

    def __init__(
        self, base: Path, *, name: str = "kplug", tool_name: str | None = None,
        config_cls: str = "None", caps: tuple[str, ...] = (), manifest: bool = True,
    ) -> None:
        self.pkg = f"kplug_{uuid.uuid4().hex[:10]}"
        self.name = name
        self.root = base / self.pkg
        self.root.mkdir(parents=True)
        (self.root / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tool.py").write_text(
            _TOOL_SRC.format(tool_name=tool_name or name, config_cls=config_cls),
            encoding="utf-8",
        )
        self.value = f"{self.pkg}.tool:PluginTool"
        self.manifest: PluginManifest | None = None
        if manifest:
            raw: dict[str, Any] = {
                "name": name, "kind": "tool", "version": "1.0.0", "version_hash": "0" * 64,
                "tier": 2, "entry": self.value, "capabilities_requested": list(caps),
            }
            raw["version_hash"] = compute_version_hash(self.root, raw)
            (self.root / MANIFEST_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
            self.manifest = PluginManifest.model_validate(raw)

    @property
    def module(self) -> str:
        return f"{self.pkg}.tool"

    def entry_point(self) -> EntryPoint:
        return EntryPoint(name=self.name, value=self.value, group="nanobot.tools")

    def imported(self) -> bool:
        return self.module in sys.modules

    def activate(self, registry: PluginRegistry) -> None:
        assert self.manifest is not None
        registry.register(self.manifest, self.root, principal=HOST)
        registry.activate(self.name, self.manifest.version_hash, HOST)


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


@pytest.fixture
def sink() -> _Sink:
    return _Sink()


def _env(tmp_path: Path, sink: _Sink, sections: dict[str, dict[str, Any]] | None = None):
    work = tmp_path / "work"
    state = tmp_path / "state"
    work.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    return CoreEnvironment(
        config=_Config(sections), credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=work, state_dir=state), trace=sink,
    )


def _ctx(tmp_path: Path, env: CoreEnvironment | None) -> ToolContext:
    return ToolContext(config=ToolsConfig(), workspace=str(tmp_path / "work"), env=env)


def _load(
    loader: ToolLoader, ctx: ToolContext, plugins: list[_Plugin],
    registry: ToolRegistry | None = None,
) -> tuple[ToolRegistry, list[str]]:
    registry = registry if registry is not None else ToolRegistry()
    eps = [p.entry_point() for p in plugins]
    with patch("nanobot.agent.tools.loader.entry_points", return_value=eps):
        names = loader.load(ctx, registry)
    return registry, names


@pytest.fixture
def plugin_registry(tmp_path: Path, sink: _Sink) -> PluginRegistry:
    return PluginRegistry(tmp_path / "state", trace=sink)


# -- registry gating ------------------------------------------------------------


def test_kernel_mode_rejects_plugin_without_manifest(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base, manifest=False)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []
    assert not registry.has("kplug")
    assert not plugin.imported(), "plugin code must not run before the registry check"
    denied = [e for e in sink.events if e.get("event") == "plugin.load"]
    assert denied and denied[-1]["verdict"] == "deny"
    assert "manifest" in denied[-1]["reason"]


def test_kernel_mode_rejects_candidate_plugin(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base)
    assert plugin.manifest is not None
    plugin_registry.register(plugin.manifest, plugin.root, principal=HOST)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []
    assert not plugin.imported()
    assert plugin_registry.get("kplug").state == "candidate"


def test_kernel_mode_rejects_unregistered_plugin(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    _registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []
    assert not plugin.imported()


def test_kernel_mode_loads_active_plugin(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base)
    plugin.activate(plugin_registry)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == ["kplug"]
    assert registry.has("kplug")
    assert plugin_registry.get("kplug").state == "active"
    # Importing the plugin must not perturb its own hash (no __pycache__ written).
    assert plugin_registry.check_active("kplug") is True
    assert not (plugin.root / "__pycache__").exists()


async def test_kernel_mode_tampered_plugin_is_quarantined_not_loaded(
    tmp_path, plugin_base, sink, plugin_registry,
):
    plugin = _Plugin(plugin_base)
    plugin.activate(plugin_registry)
    # Tamper after host activation: same manifest, different code.
    (plugin.root / "tool.py").write_text(
        (plugin.root / "tool.py").read_text(encoding="utf-8") + "\nEVIL = 1\n",
        encoding="utf-8",
    )
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []
    assert not registry.has("kplug")
    assert not plugin.imported(), "tampered code must never be imported"
    # The loader triggered Task 18's load-time auto-quarantine.
    record = plugin_registry.get("kplug")
    assert record.state == "quarantined"
    assert record.reason == "hash mismatch at load"
    lifecycle = [e for e in sink.events if e.get("event") == "plugin.lifecycle"]
    assert lifecycle[-1]["action"] == "load_check"
    assert (lifecycle[-1]["from"], lifecycle[-1]["to"]) == ("active", "quarantined")
    assert lifecycle[-1]["actor_kind"] == "host"
    assert lifecycle[-1]["actor"] == "plugin-registry"


def test_kernel_mode_hash_comes_from_disk_not_manifest_claim(
    tmp_path, plugin_base, sink, plugin_registry,
):
    """A tampered package that re-stamps its manifest with the PINNED hash still fails."""
    plugin = _Plugin(plugin_base)
    plugin.activate(plugin_registry)
    assert plugin.manifest is not None
    pinned = plugin.manifest.version_hash
    (plugin.root / "tool.py").write_text("IMPORTED = 'evil'\n", encoding="utf-8")
    raw = json.loads((plugin.root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert raw["version_hash"] == pinned  # the claim still matches the pin
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    _registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []
    assert plugin_registry.get("kplug").state == "quarantined"


def test_kernel_mode_rejects_manifest_for_other_entry(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base)
    plugin.activate(plugin_registry)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)
    other = EntryPoint(name="kplug", value=f"{plugin.pkg}.tool:Other", group="nanobot.tools")

    with patch("nanobot.agent.tools.loader.entry_points", return_value=[other]):
        names = loader.load(_ctx(tmp_path, _env(tmp_path, sink)), ToolRegistry())

    assert names == []
    assert not plugin.imported()


# -- per-plugin config ----------------------------------------------------------


def test_kernel_mode_invalid_plugin_config_is_rejected(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base, config_cls="Cfg")
    plugin.activate(plugin_registry)
    env = _env(tmp_path, sink, {"kplug": {"limit": "not-a-number-SECRETISH"}})
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, env), [plugin])

    assert names == []
    assert not registry.has("kplug")
    denied = [e for e in sink.events if e.get("event") == "plugin.load"]
    assert denied[-1]["verdict"] == "deny"
    assert "config" in denied[-1]["reason"] and "limit" in denied[-1]["reason"]
    # The error names the field, never echoes the rejected value.
    assert "SECRETISH" not in denied[-1]["reason"]


def test_kernel_mode_missing_required_plugin_config_is_rejected(
    tmp_path, plugin_base, sink, plugin_registry,
):
    plugin = _Plugin(plugin_base, config_cls="Cfg")
    plugin.activate(plugin_registry)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    _registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    assert names == []


def test_kernel_mode_valid_plugin_config_loads(tmp_path, plugin_base, sink, plugin_registry):
    plugin = _Plugin(plugin_base, config_cls="Cfg")
    plugin.activate(plugin_registry)
    env = _env(tmp_path, sink, {"kplug": {"limit": 3}})
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, env), [plugin])

    assert names == ["kplug"]
    tool = registry.get("kplug")
    assert tool.seen_config.limit == 3


# -- capability grant: policy ∩ capabilities_requested ------------------------------


async def test_kernel_mode_grant_is_policy_intersect_requested(
    tmp_path, plugin_base, sink, plugin_registry,
):
    plugin = _Plugin(
        plugin_base, caps=("net.fetch:api.example.com", "fs.write", "fs.read"),
    )
    plugin.activate(plugin_registry)
    registry = ToolRegistry()
    registry.configure_gate(policy=DefaultPolicy(deny_capabilities=frozenset({"fs.write"})))
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin], registry)

    assert names == ["kplug"]
    tool = registry.get("kplug")
    # fs.write requested but denied everywhere by policy: not granted.
    assert tool.capability_grant == ("net.fetch:api.example.com", "fs.read")
    # Surface is narrowed to the granted names.
    assert tool.capability_surface() == frozenset({"net.fetch"})

    ok = registry.gate(tool, {"cap": "net.fetch", "resource": "api.example.com"})
    assert ok.allowed
    other_host = registry.gate(tool, {"cap": "net.fetch", "resource": "evil.example.org"})
    assert not other_host.allowed
    assert "capability grant" in other_host.error_text()
    never_requested = registry.gate(tool, {"cap": "exec.run", "resource": "ls"})
    assert not never_requested.allowed
    # Declared and requested but policy-denied: still denied (by grant, then policy).
    assert not registry.gate(tool, {"cap": "fs.write", "resource": "x"}).allowed


async def test_kernel_mode_plugin_requesting_nothing_gets_nothing(
    tmp_path, plugin_base, sink, plugin_registry,
):
    plugin = _Plugin(plugin_base)
    plugin.activate(plugin_registry)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    registry, _names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])

    tool = registry.get("kplug")
    assert tool.capability_grant == ()
    assert not registry.gate(tool, {"cap": "net.fetch", "resource": "a"}).allowed
    # A call that declares nothing still runs (the gate is declaration-based).
    assert registry.gate(tool, {}).allowed
    assert await registry.execute("kplug", {}) == "plugin ran"


# -- collisions ---------------------------------------------------------------------


def test_kernel_mode_plugin_shadowing_builtin_is_load_error(
    tmp_path, plugin_base, sink, plugin_registry,
):
    plugin = _Plugin(plugin_base, tool_name="read_file")
    plugin.activate(plugin_registry)
    loader = ToolLoader(test_classes=[ReadFileTool], plugin_registry=plugin_registry)

    with pytest.raises(LoadError, match="read_file"):
        _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [plugin])


def test_kernel_mode_plugin_vs_plugin_collision_is_load_error(
    tmp_path, plugin_base, sink, plugin_registry,
):
    first = _Plugin(plugin_base, name="kplug-a", tool_name="dup_tool")
    second = _Plugin(plugin_base, name="kplug-b", tool_name="dup_tool")
    first.activate(plugin_registry)
    second.activate(plugin_registry)
    loader = ToolLoader(test_classes=[], plugin_registry=plugin_registry)

    with pytest.raises(LoadError, match="dup_tool"):
        _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [first, second])


def test_legacy_mode_plugin_shadowing_builtin_still_skipped(tmp_path, plugin_base):
    plugin = _Plugin(plugin_base, tool_name="read_file")
    loader = ToolLoader(test_classes=[ReadFileTool])

    registry, names = _load(loader, _ctx(tmp_path, None), [plugin])

    assert names == ["read_file"]
    assert isinstance(registry.get("read_file"), ReadFileTool)


# -- regression boundaries ------------------------------------------------------------


def test_legacy_mode_loads_manifest_plugin_exactly_as_before(tmp_path, plugin_base, sink):
    """No registry: manifest, registry and config_cls checks are all skipped."""
    plugin = _Plugin(plugin_base, config_cls="Cfg", caps=("net.fetch",))
    # Never registered, and its config section is invalid: legacy ignores both.
    env = _env(tmp_path, sink, {"kplug": {"limit": "bad"}})
    loader = ToolLoader(test_classes=[])

    registry, names = _load(loader, _ctx(tmp_path, env), [plugin])

    assert names == ["kplug"]
    tool = registry.get("kplug")
    assert tool.seen_config is None
    assert not hasattr(tool, "capability_grant") or tool.capability_grant is None
    assert not [e for e in sink.events if e.get("event") == "plugin.load"]


def test_legacy_mode_plugin_without_manifest_loads(tmp_path, plugin_base):
    plugin = _Plugin(plugin_base, manifest=False)

    _registry, names = _load(ToolLoader(test_classes=[]), _ctx(tmp_path, None), [plugin])

    assert names == ["kplug"]


class _BuiltinWithStrictConfig(ReadFileTool):
    """A built-in whose config_cls would reject any section: kernel mode must not call it."""

    @classmethod
    def config_cls(cls):
        raise AssertionError("config_cls must not be called for built-ins")


def test_builtins_unaffected_by_kernel_mode(tmp_path, plugin_base, sink, plugin_registry):
    builtins = [ReadFileTool, WriteFileTool, ListDirTool]
    unregistered = _Plugin(plugin_base)  # not in the registry: skipped in kernel mode
    env = _env(tmp_path, sink)

    _r1, legacy = _load(ToolLoader(test_classes=builtins), _ctx(tmp_path, env), [])
    _r2, kernel = _load(
        ToolLoader(test_classes=builtins, plugin_registry=plugin_registry),
        _ctx(tmp_path, env), [unregistered],
    )

    assert legacy == kernel == ["read_file", "write_file", "list_dir"]
    assert plugin_registry.list_plugins() == []


def test_builtin_config_cls_not_called_in_kernel_mode(tmp_path, sink, plugin_registry):
    loader = ToolLoader(test_classes=[_BuiltinWithStrictConfig], plugin_registry=plugin_registry)

    registry, names = _load(loader, _ctx(tmp_path, _env(tmp_path, sink)), [])

    assert names == ["read_file"]
    assert getattr(registry.get("read_file"), "capability_grant", None) is None


def test_builtins_discovered_identically_in_both_modes(tmp_path, sink, plugin_registry):
    def ctx() -> ToolContext:
        return ToolContext(config=ToolsConfig(), workspace=str(tmp_path / "work"),
                           env=_env(tmp_path, sink))

    _r1, legacy = _load(ToolLoader(), ctx(), [])
    _r2, kernel = _load(ToolLoader(plugin_registry=plugin_registry), ctx(), [])

    assert legacy == kernel
    assert {"read_file", "write_file", "exec"} <= set(kernel)
