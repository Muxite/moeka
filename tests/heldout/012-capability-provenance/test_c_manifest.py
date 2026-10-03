"""Part C: the script tool manifest (FR-009): ``load_manifest`` validation, one ``ManifestError``
naming the field per defect."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _h012 import (
    DEFAULT_PARAMS,
    DELETE,
    SH_OK,
    agent_of,
    cap,
    st,
    tool_names,
    write_script_tool,
)

NAME = "h012-mf"


def _load(bundle: Path):
    return st().load_manifest(bundle)


def _assert_names(bundle: Path, field: str) -> None:
    me = cap("manifest").ManifestError
    with pytest.raises(me) as info:
        _load(bundle)
    exc = info.value
    got = getattr(exc, "field", None)
    if got:
        assert str(got) == field or str(got).startswith(field + "."), (got, str(exc))
    else:
        assert field in str(exc), str(exc)


def _io_tuple(io):
    if isinstance(io, dict):
        io = cap("manifest").parse_io(io)
    return (tuple(io.inputs_all), tuple(io.inputs_any), tuple(io.outputs), tuple(io.effects))


# -- valid ----------------------------------------------------------------------------------------


@pytest.mark.fr("FR-009")
def test_valid_manifest_fields(tmp_path):
    io = {"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["network"]}
    b = write_script_tool(tmp_path / "ws", NAME, io=io, timeout_s=45,
                          output_schema={"type": "object"})
    m = _load(b)
    assert isinstance(m, st().ScriptManifest)
    assert m.name == NAME
    assert m.description == f"Script tool {NAME} for held-out tests"
    assert m.parameters == DEFAULT_PARAMS
    assert m.output_schema == {"type": "object"}
    assert m.timeout_s == 45
    assert (b / Path(m.entry)).resolve() == (b / "run").resolve()
    assert _io_tuple(m.io) == (("text/query",), (), ("data/json",), ("network",))


@pytest.mark.fr("FR-009")
def test_output_schema_is_optional(tmp_path):
    m = _load(write_script_tool(tmp_path / "ws", NAME))
    assert m.output_schema is None


@pytest.mark.fr("FR-009")
def test_entry_in_subdir_and_dotdot_inside_bundle(tmp_path):
    b = write_script_tool(tmp_path / "ws", NAME, entry="bin/../bin/tool.sh", entry_path="bin/tool.sh")
    m = _load(b)
    assert (b / Path(m.entry)).resolve() == (b / "bin" / "tool.sh").resolve()


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("value", [1, 600])
def test_timeout_bounds_inclusive(tmp_path, value):
    assert _load(write_script_tool(tmp_path / "ws", NAME, timeout_s=value)).timeout_s == value


@pytest.mark.fr("FR-009")
def test_description_at_limit(tmp_path):
    m = _load(write_script_tool(tmp_path / "ws", NAME, description="d" * 1024))
    assert len(m.description) == 1024


# -- invalid: one field each -------------------------------------------------------------------------

INVALID = {
    "name-missing": ({"name": DELETE}, "name"),
    "name-mismatch": ({"name": "h012-other"}, "name"),
    "name-not-a-string": ({"name": 7}, "name"),
    "description-missing": ({"description": DELETE}, "description"),
    "description-empty": ({"description": ""}, "description"),
    "description-too-long": ({"description": "d" * 1025}, "description"),
    "description-not-a-string": ({"description": ["x"]}, "description"),
    "parameters-missing": ({"parameters": DELETE}, "parameters"),
    "parameters-list": ({"parameters": ["q"]}, "parameters"),
    "parameters-string": ({"parameters": "object"}, "parameters"),
    "output-schema-string": ({"output_schema": "json"}, "output_schema"),
    "output-schema-list": ({"output_schema": [1]}, "output_schema"),
    "io-missing": ({"io": DELETE}, "io"),
    "io-not-a-mapping": ({"io": "untyped"}, "io"),
    "io-bad-effect": ({"io": {"effects": ["teleport"]}}, "io"),
    "io-bad-type": ({"io": {"inputs": ["file:nope"]}}, "io"),
    "entry-missing": ({"entry": DELETE}, "entry"),
    "entry-absent-file": ({"entry": "nope"}, "entry"),
    "entry-escapes-dotdot": ({"entry": "../h012-sibling/run"}, "entry"),
    "entry-escapes-deep": ({"entry": "sub/../../h012-sibling/run"}, "entry"),
    "entry-absolute": ({"entry": "/bin/sh"}, "entry"),
    "timeout-zero": ({"timeout_s": 0}, "timeout_s"),
    "timeout-negative": ({"timeout_s": -1}, "timeout_s"),
    "timeout-too-big": ({"timeout_s": 601}, "timeout_s"),
    "timeout-not-a-number": ({"timeout_s": "fast"}, "timeout_s"),
}


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_field_is_named(tmp_path, case):
    over, field = INVALID[case]
    ws = tmp_path / "ws"
    write_script_tool(ws, "h012-sibling")  # an executable a bad entry could reach
    b = write_script_tool(ws, NAME, **over)
    _assert_names(b, field)


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("name", ["Bad_Name", "h012--double", "-h012-lead", "x" * 65])
def test_name_must_follow_skill_naming_rule(tmp_path, name):
    b = write_script_tool(tmp_path / "ws", name)
    _assert_names(b, "name")


@pytest.mark.fr("FR-009")
def test_entry_symlink_rejected(tmp_path):
    b = write_script_tool(tmp_path / "ws", NAME, entry="run", entry_path="real")
    os.symlink("real", b / "run")
    _assert_names(b, "entry")


@pytest.mark.fr("FR-009")
def test_entry_symlink_to_outside_rejected(tmp_path):
    outside = tmp_path / "outside.sh"
    outside.write_text(SH_OK, encoding="utf-8")
    outside.chmod(0o755)
    b = write_script_tool(tmp_path / "ws", NAME, script=None)
    os.symlink(outside, b / "run")
    _assert_names(b, "entry")


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("mode", [0o644, 0o600, 0o444])
def test_entry_not_executable_rejected(tmp_path, mode):
    b = write_script_tool(tmp_path / "ws", NAME, mode=mode)
    _assert_names(b, "entry")


@pytest.mark.fr("FR-009")
def test_entry_directory_rejected(tmp_path):
    b = write_script_tool(tmp_path / "ws", NAME, script=None, entry="bin")
    (b / "bin").mkdir()
    (b / "bin").chmod(0o755)
    _assert_names(b, "entry")


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("text", ["name: [unclosed\n", "- just\n- a list\n", ""],
                         ids=["bad-yaml", "list", "empty"])
def test_unparseable_manifest_raises(tmp_path, text):
    b = write_script_tool(tmp_path / "ws", NAME, raw_yaml=text)
    with pytest.raises(cap("manifest").ManifestError):
        _load(b)


@pytest.mark.fr("FR-009")
def test_missing_tool_yaml_raises(tmp_path):
    b = tmp_path / "ws" / "tools-script" / NAME
    b.mkdir(parents=True)
    with pytest.raises(cap("manifest").ManifestError):
        _load(b)


@pytest.mark.fr("FR-009", "FR-010")
def test_invalid_manifest_is_not_registered(kmaker, tmp_path):
    work = tmp_path / "work"
    write_script_tool(work, "h012-good")
    write_script_tool(work, "h012-noexec", mode=0o644)
    write_script_tool(work, "h012-noio", io=DELETE)
    kernel, _ = kmaker(work=work, script_enabled=True)
    names = tool_names(agent_of(kernel))
    assert "script_h012-good" in names
    assert "script_h012-noexec" not in names
    assert "script_h012-noio" not in names
