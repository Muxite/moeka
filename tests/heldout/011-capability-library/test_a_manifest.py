"""Part A: io manifests (FR-002) and workspace skill validation (FR-003)."""

from __future__ import annotations

import dataclasses

import pytest
from _h011 import cap, write_skill

# -- FR-002 parse_io --------------------------------------------------------------------------


def _fields(spec):
    return (spec.inputs_all, spec.inputs_any, spec.outputs, spec.effects)


@pytest.mark.fr("FR-002")
def test_no_block_is_none():
    assert cap("manifest").parse_io(None) is None


@pytest.mark.fr("FR-002")
def test_shorthand_inputs_and_defaults():
    m = cap("manifest")
    spec = m.parse_io({"inputs": ["file:pdf"]})
    assert isinstance(spec, m.IOSpec)
    assert _fields(spec) == (("file:pdf",), (), ("any",), ("exec",))


@pytest.mark.fr("FR-002")
def test_dict_form_inputs():
    spec = cap("manifest").parse_io({
        "inputs": {"all": ["path"], "any": ["file:pdf", "file:office"]},
        "outputs": ["text/markdown"], "effects": ["read-workspace"],
    })
    assert _fields(spec) == (("path",), ("file:office", "file:pdf"), ("text/markdown",),
                             ("read-workspace",))


@pytest.mark.fr("FR-002")
def test_dict_form_with_only_any():
    spec = cap("manifest").parse_io({"inputs": {"any": ["url/git", "repo:git"]}})
    assert spec.inputs_all == ()
    assert spec.inputs_any == ("repo:git", "url/git")


@pytest.mark.fr("FR-002")
def test_dict_form_with_only_all():
    spec = cap("manifest").parse_io({"inputs": {"all": ["data/csv"]}})
    assert spec.inputs_all == ("data/csv",) and spec.inputs_any == ()


@pytest.mark.fr("FR-002")
def test_empty_block_takes_all_defaults():
    spec = cap("manifest").parse_io({})
    assert _fields(spec) == ((), (), ("any",), ("exec",))


@pytest.mark.fr("FR-002")
def test_missing_inputs_is_empty():
    spec = cap("manifest").parse_io({"outputs": ["data/json"], "effects": ["none"]})
    assert _fields(spec) == ((), (), ("data/json",), ("none",))


@pytest.mark.fr("FR-002")
def test_all_fields_are_sorted_tuples():
    spec = cap("manifest").parse_io({
        "inputs": {"all": ["workspace", "data/csv", "path"], "any": ["url/web", "file:pdf"]},
        "outputs": ["text/markdown", "data/csv", "file:image"],
        "effects": ["write-workspace", "network", "exec", "read-workspace"],
    })
    for value in _fields(spec):
        assert isinstance(value, tuple)
        assert list(value) == sorted(value)
    assert spec.inputs_all == ("data/csv", "path", "workspace")
    assert spec.effects == ("exec", "network", "read-workspace", "write-workspace")


@pytest.mark.fr("FR-002")
def test_iospec_is_frozen_and_equal_by_value():
    m = cap("manifest")
    a = m.parse_io({"inputs": ["file:pdf", "path"], "outputs": ["none"], "effects": ["sudo"]})
    b = m.parse_io({"inputs": {"all": ["path", "file:pdf"]}, "outputs": ["none"],
                    "effects": ["sudo"]})
    assert dataclasses.is_dataclass(a)
    assert a == b and hash(a) == hash(b)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.outputs = ("any",)


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("effects", [["none"], ["read-workspace"], ["write-workspace"],
                                     ["network"], ["exec"], ["sudo"],
                                     ["none", "read-workspace", "write-workspace", "network",
                                      "exec", "sudo"]])
def test_every_effect_value_is_accepted(effects):
    spec = cap("manifest").parse_io({"effects": effects})
    assert spec.effects == tuple(sorted(effects))


@pytest.mark.fr("FR-002")
def test_none_is_a_valid_output():
    spec = cap("manifest").parse_io({"outputs": ["none"], "effects": ["write-workspace"]})
    assert spec.outputs == ("none",)


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("raw, field", [
    ({"inputs": ["file:bogus"]}, "inputs"),
    ({"inputs": {"all": ["bogus"]}}, "inputs"),
    ({"inputs": {"any": ["file:pdf", "nope/nope"]}}, "inputs"),
    ({"outputs": ["text/nope"]}, "outputs"),
    ({"inputs": ["none"]}, "inputs"),
    ({"inputs": {"all": ["none"]}}, "inputs"),
    ({"inputs": {"any": ["none", "file:pdf"]}}, "inputs"),
    ({"effects": ["delete-everything"]}, "effects"),
    ({"effects": ["exec", "Network"]}, "effects"),
    ({"outputs": "text/markdown"}, "outputs"),
    ({"effects": "exec"}, "effects"),
    ({"inputs": "file:pdf"}, "inputs"),
    ({"inputs": 3}, "inputs"),
    ({"inputs": {"all": "path"}}, "inputs"),
    ({"inputs": {"any": "file:pdf"}}, "inputs"),
    ({"outputs": {"all": ["data/csv"]}}, "outputs"),
    ({"outputs": ["data/csv", "data/csv"]}, "outputs"),
    ({"effects": ["exec", "exec"]}, "effects"),
    ({"inputs": ["path", "path"]}, "inputs"),
    ({"inputs": {"any": ["file:pdf", "file:pdf"]}}, "inputs"),
])
def test_manifest_errors_name_the_field(raw, field):
    m = cap("manifest")
    assert issubclass(m.ManifestError, Exception)
    with pytest.raises(m.ManifestError) as info:
        m.parse_io(raw)
    assert field in str(info.value)


# -- FR-003 validate_skill ---------------------------------------------------------------------


@pytest.mark.fr("FR-003")
def test_valid_skill_without_io(tmp_path):
    path = write_skill(tmp_path, "plain-skill", "A plain skill")
    assert cap("manifest").validate_skill(path) == []


@pytest.mark.fr("FR-003")
def test_valid_skill_with_io(tmp_path):
    path = write_skill(tmp_path, "typed-skill", "A typed skill",
                       io={"inputs": {"all": ["path"], "any": ["file:pdf"]},
                           "outputs": ["text/markdown"], "effects": ["read-workspace"]})
    assert cap("manifest").validate_skill(path) == []


def _bad_skills(root):
    """name -> SKILL.md path of skills each violating exactly one FR-003 rule."""
    out = {}
    out["bad-io-type"] = write_skill(root, "bad-io-type", "bad io", io={"inputs": ["file:nope"]})
    out["bad-io-effect"] = write_skill(root, "bad-io-effect", "bad effect",
                                       io={"effects": ["teleport"]})
    out["bad-io-none-input"] = write_skill(root, "bad-io-none-input", "none input",
                                           io={"inputs": ["none"]})
    out["bad-io-scalar"] = write_skill(root, "bad-io-scalar", "scalar outputs",
                                       io={"outputs": "text/markdown"})
    out["bad-io-dup"] = write_skill(root, "bad-io-dup", "dup", io={"outputs": ["data/csv",
                                                                               "data/csv"]})
    out["name-mismatch"] = write_skill(root, "name-mismatch", "mismatch", front_name="other")
    out["no-description"] = write_skill(root, "no-description", None)
    out["long-description"] = write_skill(root, "long-description", "x" * 1025)
    out["Bad_Name"] = write_skill(root, "Bad_Name", "bad dir name")
    d = root / "no-frontmatter"
    d.mkdir()
    (d / "SKILL.md").write_text("# no frontmatter\n\nbody\n", encoding="utf-8")
    out["no-frontmatter"] = d / "SKILL.md"
    return out


@pytest.mark.fr("FR-003")
def test_each_invalid_skill_reports_problems(tmp_path):
    m = cap("manifest")
    for name, path in _bad_skills(tmp_path).items():
        problems = m.validate_skill(path)
        assert isinstance(problems, list) and problems, name
        assert all(isinstance(p, str) and p for p in problems), name


@pytest.mark.fr("FR-003")
def test_description_at_limit_is_valid(tmp_path):
    path = write_skill(tmp_path, "edge-skill", "y" * 1024)
    assert cap("manifest").validate_skill(path) == []


def _loader(work, builtin):
    from nanobot.agent.skills import SkillsLoader

    return SkillsLoader(work, builtin_skills_dir=builtin)


@pytest.mark.fr("FR-003")
def test_loader_skips_invalid_workspace_skills_and_warns(tmp_path, logs):
    work = tmp_path / "work"
    builtin = tmp_path / "bskills"
    write_skill(builtin, "builtin-ok", "builtin skill")
    write_skill(work / "skills", "good-one", "A good workspace skill",
                io={"inputs": ["file:pdf"], "outputs": ["text/markdown"], "effects": ["none"]})
    write_skill(work / "skills", "good-untyped", "Another good one")
    bad = _bad_skills(work / "skills")
    loader = _loader(work, builtin)
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert {"good-one", "good-untyped", "builtin-ok"} <= names
    assert not names & set(bad)
    for name in bad:
        assert loader.load_skill(name) is None, name
    summary = loader.build_skills_summary()
    assert "good-one" in summary and "builtin-ok" in summary
    for name in bad:
        assert f"**{name}**" not in summary, name
    m = cap("manifest")
    warned = [msg for level, msg in logs if level == "WARNING"]
    for name, path in bad.items():
        first = m.validate_skill(path)[0]
        hits = [msg for msg in warned if name in msg]
        assert hits, f"no warning names {name}"
        assert any(first in msg for msg in hits), f"warning for {name} lacks its first problem"
    assert not any("good-one" in msg or "good-untyped" in msg for msg in warned)


@pytest.mark.fr("FR-003")
def test_invalid_workspace_skill_is_not_explicitly_invocable(tmp_path):
    work = tmp_path / "work"
    write_skill(work / "skills", "bad-io-type", "bad io", io={"inputs": ["file:nope"]})
    loader = _loader(work, tmp_path / "empty-builtin")
    assert loader.get_explicitly_invoked_skills("use $bad-io-type now") == []


@pytest.mark.fr("FR-003")
def test_builtin_skills_are_not_validated_at_runtime(tmp_path):
    builtin = tmp_path / "bskills"
    write_skill(builtin, "builtin-bad-io", "builtin with a bad io block",
                io={"inputs": ["file:nope"]})
    write_skill(builtin, "builtin-ok", "fine")
    loader = _loader(tmp_path / "work", builtin)
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert {"builtin-bad-io", "builtin-ok"} <= names
    assert loader.load_skill("builtin-bad-io") is not None


@pytest.mark.fr("FR-003")
def test_valid_workspace_skill_with_io_still_shadows_builtin(tmp_path):
    builtin = tmp_path / "bskills"
    work = tmp_path / "work"
    write_skill(builtin, "shared", "builtin version")
    write_skill(work / "skills", "shared", "workspace version",
                io={"inputs": ["path"], "effects": ["read-workspace"]})
    loader = _loader(work, builtin)
    entries = [s for s in loader.list_skills(filter_unavailable=False) if s["name"] == "shared"]
    assert [e["source"] for e in entries] == ["workspace"]
