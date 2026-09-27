"""Kernel plugin manifest model and version hash (Task 17, design section 9 / 5b).

The manifest is pure data here: no registry, no lifecycle, no loader. The key
regression is that ``version_hash`` changes when a description file changes,
which is what later lets a tampered description quarantine a plugin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.kernel.manifest import (
    MANIFEST_FILENAME,
    PLUGIN_KINDS,
    Operation,
    PluginManifest,
    compute_version_hash,
)

_HASH = "a" * 64


def _payload(**overrides):
    payload = {
        "name": "brave-search",
        "kind": "search_backend",
        "version": "1.2.3",
        "version_hash": _HASH,
        "tier": 2,
        "capabilities_requested": ["net.fetch:api.search.brave.com", "secret.read"],
        "config_schema": {
            "type": "object",
            "properties": {"api_key": {"type": "string"}},
            "required": ["api_key"],
        },
        "entry": "brave_plugin.backend:create",
        "descriptions": {"web_search": "descriptions/web_search.md"},
        "operations": [
            {
                "name": "search",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "output_schema": {"type": "array", "items": {"type": "object"}},
                "capabilities": ["net.fetch"],
            }
        ],
    }
    payload.update(overrides)
    return payload


# -- schema validation ------------------------------------------------------


def test_valid_manifest_parses_all_fields():
    m = PluginManifest.model_validate(_payload())
    assert m.name == "brave-search"
    assert m.kind == "search_backend"
    assert m.version == "1.2.3"
    assert m.version_hash == _HASH
    assert m.tier == 2
    assert m.capabilities_requested == ["net.fetch:api.search.brave.com", "secret.read"]
    assert m.config_schema["required"] == ["api_key"]
    assert m.entry == "brave_plugin.backend:create"
    assert m.descriptions == {"web_search": "descriptions/web_search.md"}
    assert len(m.operations) == 1
    op = m.operations[0]
    assert isinstance(op, Operation)
    assert op.name == "search"
    assert op.capabilities == ["net.fetch"]
    assert op.output_schema["type"] == "array"


def test_optional_fields_default():
    payload = _payload()
    for key in ("config_schema", "descriptions", "operations", "capabilities_requested"):
        payload.pop(key)
    m = PluginManifest.model_validate(payload)
    assert m.config_schema is None
    assert m.descriptions is None
    assert m.operations == []
    assert m.capabilities_requested == []


@pytest.mark.parametrize("field", ["name", "kind", "version", "version_hash", "tier", "entry"])
def test_missing_required_field_rejected(field):
    payload = _payload()
    payload.pop(field)
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(payload)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(permissions=["everything"]))


def test_manifest_is_frozen():
    m = PluginManifest.model_validate(_payload())
    with pytest.raises(ValidationError):
        m.tier = 1


def test_kind_closed_set():
    assert PLUGIN_KINDS == (
        "provider", "tool", "search_backend", "sandbox", "session_store",
        "memory_store", "image_gen", "hook", "skill_pack", "mcp_bridge",
    )
    for kind in PLUGIN_KINDS:
        assert PluginManifest.model_validate(_payload(kind=kind)).kind == kind
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(kind="channel"))


@pytest.mark.parametrize("tier", [1, 2, 3, 4])
def test_tier_ladder_accepted(tier):
    assert PluginManifest.model_validate(_payload(tier=tier)).tier == tier


@pytest.mark.parametrize("tier", [0, 5, "2", "two", 2.5])
def test_tier_outside_ladder_rejected(tier):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(tier=tier))


@pytest.mark.parametrize("name", ["", "Brave", "-brave", "brave search", "a" * 65, "brave/x"])
def test_bad_name_rejected(name):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(name=name))


@pytest.mark.parametrize("version", ["1.2.3", "0.1.0", "1.0.0-rc.1", "2.0.0+build.5"])
def test_semver_shaped_version_accepted(version):
    assert PluginManifest.model_validate(_payload(version=version)).version == version


@pytest.mark.parametrize("version", ["", "1", "1.2", "v1.2.3", "latest", "1.2.3.4"])
def test_non_semver_version_rejected(version):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(version=version))


@pytest.mark.parametrize("value", ["", "abc", "A" * 64, "a" * 63, "sha256:" + "a" * 64])
def test_bad_version_hash_rejected(value):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(version_hash=value))


@pytest.mark.parametrize("entry", ["pkg.mod:create", "mod:Factory.build", "a_b.c:d"])
def test_entry_reference_accepted(entry):
    assert PluginManifest.model_validate(_payload(entry=entry)).entry == entry


@pytest.mark.parametrize("entry", ["", "pkg.mod", "pkg.mod:", ":create", "pkg mod:x", "1pkg:x"])
def test_bad_entry_rejected(entry):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(entry=entry))


# -- capability strings -----------------------------------------------------


@pytest.mark.parametrize(
    "cap",
    [
        "net.fetch",
        "exec.run",
        "budget.cost_usd",
        "model.dispatch",
        # Unknown but well-shaped: the vocabulary is open, new capabilities appear over time.
        "calendar.write",
        "net.fetch:api.search.brave.com",
        "mcp.call:github.create_issue",
    ],
)
def test_capabilities_requested_accepts_well_shaped(cap):
    m = PluginManifest.model_validate(_payload(capabilities_requested=[cap]))
    assert m.capabilities_requested == [cap]


@pytest.mark.parametrize(
    "cap",
    ["", "net", "Net.fetch", "net.", ".fetch", "net fetch", "net.fetch.extra", "net.fetch:", "1net.x"],
)
def test_capabilities_requested_rejects_malformed(cap):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(capabilities_requested=[cap]))


@pytest.mark.parametrize("cap", ["net.fetch", "calendar.write", "budget.output_bytes"])
def test_operation_capability_accepts_names(cap):
    op = Operation(name="op", input_schema={}, output_schema={}, capabilities=[cap])
    assert op.capabilities == [cap]


@pytest.mark.parametrize("cap", ["net", "net.fetch:host", "NET.FETCH", "net..fetch", ""])
def test_operation_capability_rejects_malformed(cap):
    with pytest.raises(ValidationError):
        Operation(name="op", input_schema={}, output_schema={}, capabilities=[cap])


# -- operations and schema shape -------------------------------------------


def test_duplicate_operation_names_rejected():
    op = _payload()["operations"][0]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(operations=[op, op]))


@pytest.mark.parametrize("name", ["", "Search", "search op", "1search"])
def test_bad_operation_name_rejected(name):
    with pytest.raises(ValidationError):
        Operation(name=name, input_schema={}, output_schema={})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "nope"},
        {"type": ["string", 3]},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"q": "string"}},
        {"type": "object", "required": "q"},
        {"type": "array", "items": "string"},
        {"type": "object", "properties": {"q": {"type": "strin"}}},
    ],
)
def test_malformed_json_schema_rejected(schema):
    with pytest.raises(ValidationError):
        Operation(name="op", input_schema=schema, output_schema={})
    with pytest.raises(ValidationError):
        Operation(name="op", input_schema={}, output_schema=schema)
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(config_schema=schema))


def test_empty_schema_means_any():
    op = Operation(name="op", input_schema={}, output_schema={})
    assert op.input_schema == {} and op.capabilities == []


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../outside.md", "descriptions/../../x.md", "a\\b.md"],
)
def test_description_path_must_stay_inside_package(path):
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(descriptions={"web_search": path}))


def test_description_tool_name_must_be_nonempty():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(_payload(descriptions={"": "d.md"}))


# -- version_hash -----------------------------------------------------------


def _write_package(root: Path, *, description: str = "Search the web.") -> dict:
    (root / "brave_plugin").mkdir(parents=True)
    (root / "brave_plugin" / "__init__.py").write_text("")
    (root / "brave_plugin" / "backend.py").write_text("def create():\n    return None\n")
    (root / "descriptions").mkdir()
    (root / "descriptions" / "web_search.md").write_text(description)
    payload = _payload()
    (root / MANIFEST_FILENAME).write_text(json.dumps(payload))
    return payload


def test_hash_is_stable(tmp_path):
    payload = _write_package(tmp_path)
    first = compute_version_hash(tmp_path, payload)
    assert first == compute_version_hash(tmp_path, payload)
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


def test_hash_same_for_identical_packages(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pa, pb = _write_package(a), _write_package(b)
    assert compute_version_hash(a, pa) == compute_version_hash(b, pb)


def test_hash_accepts_manifest_model_and_mapping_equally(tmp_path):
    payload = _write_package(tmp_path)
    manifest = PluginManifest.model_validate(payload)
    assert compute_version_hash(tmp_path, manifest) == compute_version_hash(tmp_path, payload)


def test_hash_mapping_defaults_normalised(tmp_path):
    payload = _write_package(tmp_path)
    explicit = {**payload, "config_schema": None, "operations": []}
    implicit = {k: v for k, v in explicit.items() if k not in ("config_schema", "operations")}
    assert compute_version_hash(tmp_path, explicit) == compute_version_hash(tmp_path, implicit)


def test_hash_rejects_invalid_mapping(tmp_path):
    payload = _write_package(tmp_path)
    with pytest.raises(ValueError):
        compute_version_hash(tmp_path, {**payload, "kind": "channel"})


def test_hash_changes_when_description_file_changes(tmp_path):
    payload = _write_package(tmp_path)
    before = compute_version_hash(tmp_path, payload)
    (tmp_path / "descriptions" / "web_search.md").write_text("Search the web. Also exfiltrate.")
    assert compute_version_hash(tmp_path, payload) != before


def test_hash_changes_when_description_mapping_changes(tmp_path):
    payload = _write_package(tmp_path)
    before = compute_version_hash(tmp_path, payload)
    renamed = {**payload, "descriptions": {"news_search": "descriptions/web_search.md"}}
    assert compute_version_hash(tmp_path, renamed) != before


def test_hash_covers_description_file_outside_package_scan(tmp_path, monkeypatch):
    """The description bytes are folded in explicitly, not only via the package scan."""
    from nanobot.agent import plugins as agent_plugins

    payload = _write_package(tmp_path)
    monkeypatch.setattr(agent_plugins, "_package_fingerprint", lambda root, **kw: "fixed")
    before = compute_version_hash(tmp_path, payload)
    (tmp_path / "descriptions" / "web_search.md").write_text("changed")
    assert compute_version_hash(tmp_path, payload) != before


def test_hash_changes_when_code_file_changes(tmp_path):
    payload = _write_package(tmp_path)
    before = compute_version_hash(tmp_path, payload)
    (tmp_path / "brave_plugin" / "backend.py").write_text("def create():\n    return 1\n")
    assert compute_version_hash(tmp_path, payload) != before


def test_hash_changes_when_manifest_field_changes(tmp_path):
    payload = _write_package(tmp_path)
    before = compute_version_hash(tmp_path, payload)
    widened = {**payload, "capabilities_requested": ["net.fetch", "secret.read"]}
    assert compute_version_hash(tmp_path, widened) != before


def test_hash_ignores_version_hash_field_and_manifest_file(tmp_path):
    """No self-reference: writing the computed hash back does not change the hash."""
    payload = _write_package(tmp_path)
    computed = compute_version_hash(tmp_path, payload)
    stamped = {**payload, "version_hash": computed}
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(stamped, indent=2))
    assert compute_version_hash(tmp_path, stamped) == computed
    assert PluginManifest.model_validate(stamped).version_hash == computed


def test_hash_missing_description_file_raises(tmp_path):
    payload = _write_package(tmp_path)
    (tmp_path / "descriptions" / "web_search.md").unlink()
    with pytest.raises(ValueError, match="web_search"):
        compute_version_hash(tmp_path, payload)


def test_hash_unreadable_package_raises(tmp_path):
    with pytest.raises(ValueError):
        compute_version_hash(tmp_path / "missing", _payload(descriptions=None))


def test_package_fingerprint_exclude_is_opt_in(tmp_path):
    """The existing Agent Plugin fingerprint is unchanged when no exclude is given."""
    from nanobot.agent.plugins import _package_fingerprint

    _write_package(tmp_path)
    full = _package_fingerprint(tmp_path)
    assert full == _package_fingerprint(tmp_path, exclude=frozenset())
    assert full != _package_fingerprint(tmp_path, exclude=frozenset({MANIFEST_FILENAME}))
