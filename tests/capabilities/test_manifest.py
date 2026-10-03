"""``io`` manifests and SKILL.md validation (spec 011 FR-002, FR-003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.capabilities.manifest import IOSpec, ManifestError, parse_io, validate_skill


def test_none_is_untyped():
    assert parse_io(None) is None


def test_shorthand_inputs_and_defaults():
    spec = parse_io({"inputs": ["file:pdf"]})
    assert spec == IOSpec(inputs_all=("file:pdf",), inputs_any=(), outputs=("any",),
                          effects=("exec",))


def test_empty_block_takes_conservative_defaults():
    assert parse_io({}) == IOSpec((), (), ("any",), ("exec",))


def test_full_block_is_normalised_to_sorted_tuples():
    spec = parse_io({
        "inputs": {"all": ["path"], "any": ["file:pdf", "file:office"]},
        "outputs": ["text/markdown", "data/csv"],
        "effects": ["write-workspace", "read-workspace"],
    })
    assert spec.inputs_all == ("path",)
    assert spec.inputs_any == ("file:office", "file:pdf")
    assert spec.outputs == ("data/csv", "text/markdown")
    assert spec.effects == ("read-workspace", "write-workspace")


def test_iospec_is_frozen_and_round_trips():
    spec = parse_io({"inputs": {"any": ["url"]}, "outputs": ["none"], "effects": ["none"]})
    with pytest.raises(AttributeError):
        spec.outputs = ()  # type: ignore[misc]
    assert parse_io(spec.to_dict()) == spec
    assert parse_io(spec) is spec


@pytest.mark.parametrize(("raw", "field"), [
    ({"inputs": ["file:docx"]}, "io.inputs"),
    ({"inputs": {"all": ["nope"]}}, "io.inputs.all"),
    ({"inputs": {"any": ["nope"]}}, "io.inputs.any"),
    ({"outputs": ["nope"]}, "io.outputs"),
    ({"inputs": ["none"]}, "io.inputs"),
    ({"inputs": {"any": ["none"]}}, "io.inputs.any"),
    ({"effects": ["teleport"]}, "io.effects"),
    ({"effects": "exec"}, "io.effects"),
    ({"outputs": "text"}, "io.outputs"),
    ({"inputs": "file:pdf"}, "io.inputs"),
    ({"inputs": {"all": "path"}}, "io.inputs.all"),
    ({"inputs": ["path", "path"]}, "io.inputs"),
    ({"outputs": ["text", "text"]}, "io.outputs"),
    ({"effects": ["exec", "exec"]}, "io.effects"),
    ({"inputs": {"some": ["path"]}}, "io.inputs.some"),
    ({"input": ["path"]}, "io.input"),
    ({"inputs": [1]}, "io.inputs"),
])
def test_invalid_blocks_name_the_field(raw, field):
    with pytest.raises(ManifestError) as info:
        parse_io(raw)
    assert str(info.value).startswith(field + ":")
    assert info.value.field == field


def test_non_mapping_block_is_rejected():
    with pytest.raises(ManifestError, match="^io:"):
        parse_io(["file:pdf"])


def test_none_is_a_valid_output():
    assert parse_io({"outputs": ["none"]}).outputs == ("none",)


def _skill(root: Path, name: str, front: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\n{front}---\n\nBody.\n", encoding="utf-8")
    return path


def test_validate_skill_accepts_a_valid_skill(tmp_path):
    path = _skill(tmp_path, "pdf-sum", "name: pdf-sum\ndescription: Summarise PDFs.\n"
                  "io:\n  inputs: [file:pdf]\n  outputs: [text/markdown]\n")
    assert validate_skill(path) == []
    assert validate_skill(path.parent) == []


def test_validate_skill_without_io_is_valid(tmp_path):
    assert validate_skill(_skill(tmp_path, "plain", "name: plain\ndescription: d\n")) == []


@pytest.mark.parametrize(("name", "front", "needle"), [
    ("alpha", "name: beta\ndescription: d\n", "does not match"),
    ("alpha", "description: d\n", "does not match"),
    ("alpha", "name: alpha\n", "description"),
    ("alpha", "name: alpha\ndescription: ''\n", "description"),
    ("Bad_Name", "name: Bad_Name\ndescription: d\n", "hyphen-case"),
    ("alpha", "name: alpha\ndescription: d\nio:\n  inputs: [file:docx]\n", "io.inputs"),
    ("alpha", "name: alpha\ndescription: d\nio: [file:pdf]\n", "invalid io block"),
])
def test_validate_skill_reports_problems(tmp_path, name, front, needle):
    problems = validate_skill(_skill(tmp_path, name, front))
    assert problems and any(needle in problem for problem in problems), problems


def test_validate_skill_without_frontmatter(tmp_path):
    path = tmp_path / "alpha" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("# no frontmatter\n", encoding="utf-8")
    assert validate_skill(path)


def test_validate_skill_missing_file(tmp_path):
    assert validate_skill(tmp_path / "ghost" / "SKILL.md")


def test_long_description_is_a_problem(tmp_path):
    path = _skill(tmp_path, "alpha", f"name: alpha\ndescription: {'x' * 1025}\n")
    assert validate_skill(path)
