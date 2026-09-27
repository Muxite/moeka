"""Kernel plugin manifest and typed operations (design section 9 and 5b, Task 17).

Pure data model plus the version hash. Nothing here registers, activates, loads or
enforces anything: the registry and lifecycle (candidate / quarantined / active /
retired) are Task 18, loader integration is Task 19.

Not the Agent Plugin model:

- :class:`nanobot.agent.plugins.AgentPlugin` is an Agents Plugins v1 package
  (``plugin.json``: skill packs and MCP-server bundles installed in a workspace).
  Its ``permissions`` are parsed and never read.
- :class:`PluginManifest` here is the kernel's host-activated plugin manifest
  (``moeka-plugin.json``) for every plugin kind in :data:`PLUGIN_KINDS`. The two do
  not share a file name, a class or a schema. Only the content fingerprint
  (``_package_fingerprint``) is reused.

Capability strings:

- The vocabulary is open (``exec.run``, ``fs.read``, ``model.dispatch`` ... were added
  task by task), so only the shape is checked: ``family.action``, lowercase
  ``[a-z][a-z0-9_]*`` on each side.
- ``capabilities_requested`` entries may carry a resource: ``net.fetch:api.example.com``.
  ``Operation.capabilities`` are bare names; the gate checks the resource per call.
- ``capabilities_requested`` is an UPPER BOUND. The effective grant is
  ``policy ∩ requested``: a plugin that requests nothing gets nothing. Enforcement
  belongs to the gate and loader, not to this model.

JSON Schema fields (``input_schema``, ``output_schema``, ``config_schema``):

- Checked for structural sanity only (``type`` is a known JSON type, ``properties``
  is a mapping of schemas, ``required`` is a list of strings, ``items`` is a schema),
  recursively. That is the subset ``Schema.validate_json_schema_value`` understands.
- Full JSON-Schema meta-validation is deferred: kernel modules import only stdlib,
  pydantic and loguru at module level, and ``jsonschema`` is not a direct dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_FILENAME = "moeka-plugin.json"

PluginKind = Literal[
    "provider", "tool", "search_backend", "sandbox", "session_store",
    "memory_store", "image_gen", "hook", "skill_pack", "mcp_bridge",
]
PLUGIN_KINDS: tuple[str, ...] = get_args(PluginKind)

# Tier ladder (design section 9):
# 1 skills, prompts, tool descriptions, tuning config -- automated edits behind the harness gate
# 2 plugin code -- sandboxed and gated, not in RSI v1
# 3 core code -- human-reviewed proposals only
# 4 policy, sandbox, evaluator, resolver, plugin list and hash pins -- never self-editable
PluginTier = Literal[1, 2, 3, 4]

_CAP_PART = r"[a-z][a-z0-9_]*"
CAPABILITY_NAME = re.compile(rf"{_CAP_PART}\.{_CAP_PART}")
CAPABILITY_RULE = re.compile(rf"{_CAP_PART}\.{_CAP_PART}(?::[^\s:][^\r\n]*)?")

_PLUGIN_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_OPERATION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DOTTED = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
_ENTRY = re.compile(rf"{_DOTTED}:{_DOTTED}")

_JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_HASH_DOMAIN = b"moeka.kernel.plugin-manifest.v1\0"


def _check_schema(schema: Any, path: str = "$") -> None:
    """Raise ``ValueError`` when ``schema`` is structurally not a JSON Schema."""
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be an object")
    if "type" in schema:
        t = schema["type"]
        types = t if isinstance(t, list) else [t]
        if not types or any(not isinstance(x, str) or x not in _JSON_TYPES for x in types):
            raise ValueError(f"{path}.type: expected one of {sorted(_JSON_TYPES)}, got {t!r}")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise ValueError(f"{path}.properties: must be an object")
        for key, sub in props.items():
            _check_schema(sub, f"{path}.properties.{key}")
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or any(not isinstance(x, str) for x in req):
            raise ValueError(f"{path}.required: must be a list of strings")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")


def _check_capabilities(values: list[str], pattern: re.Pattern[str], what: str) -> list[str]:
    for value in values:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"malformed {what} {value!r}: expected 'family.action'")
    return values


class Operation(BaseModel):
    """One typed operation a service plugin exposes (design section 5b).

    The gate checks ``capabilities``; the kernel checks ``input_schema`` before the
    call and ``output_schema`` before the result reaches the model. An empty schema
    (``{}``) accepts any value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if _OPERATION_NAME.fullmatch(v) is None:
            raise ValueError(f"operation name {v!r} must match [a-z][a-z0-9_]*")
        return v

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        _check_schema(v)
        return v

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        return _check_capabilities(v, CAPABILITY_NAME, "capability name")


class PluginManifest(BaseModel):
    """A kernel plugin's declared identity, trust tier and capability upper bound.

    Fields follow design section 9. ``version_hash`` is the value from
    :func:`compute_version_hash`; the host pins it and a mismatch at load quarantines
    the plugin (Task 18/19). ``descriptions`` maps a tool name to a description file,
    a root-relative POSIX path inside the package. ``config_schema`` is the plugin's
    config section as a JSON Schema (``None``: no config). ``entry`` is a
    ``module:attr`` object reference, the ``nanobot.tools`` entry-point format.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: PluginKind
    version: str
    version_hash: str
    tier: PluginTier
    capabilities_requested: list[str] = Field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    entry: str
    descriptions: dict[str, str] | None = None
    operations: list[Operation] = Field(default_factory=list)

    @field_validator("tier", mode="before")
    @classmethod
    def _tier_strict(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("tier must be an integer 1-4")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if _PLUGIN_NAME.fullmatch(v) is None:
            raise ValueError(f"plugin name {v!r} must match [a-z0-9][a-z0-9_-]{{0,63}}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if _SEMVER.fullmatch(v) is None:
            raise ValueError(f"version {v!r} must be MAJOR.MINOR.PATCH[-pre][+build]")
        return v

    @field_validator("version_hash")
    @classmethod
    def _version_hash(cls, v: str) -> str:
        if _SHA256_HEX.fullmatch(v) is None:
            raise ValueError("version_hash must be 64 lowercase hex characters (sha256)")
        return v

    @field_validator("entry")
    @classmethod
    def _entry(cls, v: str) -> str:
        if _ENTRY.fullmatch(v) is None:
            raise ValueError(f"entry {v!r} must be a 'module:attr' reference")
        return v

    @field_validator("capabilities_requested")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        return _check_capabilities(v, CAPABILITY_RULE, "capability rule")

    @field_validator("config_schema")
    @classmethod
    def _config_schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            _check_schema(v)
        return v

    @field_validator("descriptions")
    @classmethod
    def _descriptions(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        for tool, rel in (v or {}).items():
            if not tool:
                raise ValueError("description tool name must be non-empty")
            _check_relative(rel)
        return v

    @model_validator(mode="after")
    def _unique_operations(self) -> PluginManifest:
        names = [op.name for op in self.operations]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate operation names: {dupes}")
        return self


def _check_relative(rel: str) -> None:
    if not rel or "\\" in rel:
        raise ValueError(f"description path {rel!r} must be a non-empty POSIX path")
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"description path {rel!r} must stay inside the package")


def compute_version_hash(root: Path, manifest: PluginManifest | Mapping[str, Any]) -> str:
    """sha256 over the package, the manifest and every description file.

    Extends ``nanobot.agent.plugins._package_fingerprint`` (paths, link targets and
    file bytes under ``root``) with:

    - the manifest content as canonical JSON, minus ``version_hash`` (so widening
      ``capabilities_requested`` changes the hash), with the ``moeka-plugin.json``
      file excluded from the package scan (no self-reference once stamped);
    - each ``descriptions`` entry, sorted by tool name: the tool name, the path and
      the file bytes. A changed description therefore changes the hash even if a
      later scan excluded that file.

    ``manifest`` may be a :class:`PluginManifest` or the raw mapping (a packager
    computes the hash before it has one to stamp); a mapping is validated through the
    model first. Raises ``ValueError`` (a pydantic ``ValidationError`` is one) when
    the manifest is invalid or the package or a description file cannot be read.
    """
    from nanobot.agent import plugins as agent_plugins  # lazy: Ruling C

    if not isinstance(manifest, PluginManifest):
        # Normalise the raw mapping through the model (defaults filled, fields checked)
        # so a mapping and its parsed manifest always hash alike.
        manifest = PluginManifest.model_validate({**manifest, "version_hash": "0" * 64})
    payload: dict[str, Any] = manifest.model_dump(mode="json")
    payload.pop("version_hash", None)

    if not root.is_dir():
        raise ValueError(f"plugin package {root} is not a directory")
    fingerprint = agent_plugins._package_fingerprint(
        root, exclude=frozenset({MANIFEST_FILENAME}),
    )
    if fingerprint is None:
        raise ValueError(f"plugin package {root} could not be fingerprinted")

    digest = hashlib.sha256(_HASH_DOMAIN)
    digest.update(fingerprint.encode())
    digest.update(b"\0manifest\0")
    digest.update(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    descriptions = payload.get("descriptions") or {}
    for tool in sorted(descriptions):
        rel = descriptions[tool]
        try:
            data = (root / rel).read_bytes()
        except OSError as exc:
            raise ValueError(f"description file for {tool!r} unreadable: {rel}") from exc
        digest.update(b"\0description\0")
        digest.update(tool.encode())
        digest.update(b"\0")
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


__all__ = [
    "CAPABILITY_NAME",
    "CAPABILITY_RULE",
    "MANIFEST_FILENAME",
    "PLUGIN_KINDS",
    "Operation",
    "PluginKind",
    "PluginManifest",
    "PluginTier",
    "compute_version_hash",
]
