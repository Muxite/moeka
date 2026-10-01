"""K1: skills in the fingerprint (FR-001 to FR-006, SC-002, US1)."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from _h006 import (
    ev,
    file_records,
    fingerprint_digest,
    inline_record,
    skills_component,
    write_skill,
)

from moeka.agents import AgentSpec
from moeka.testing import FakeProvider
from moeka.variants import Fingerprint, Variant

FIVE = {"system_prompt", "tools", "model", "sampling", "skills"}
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
UNAVAILABLE = 'metadata: {"nanobot": {"requires": {"bins": ["h006-no-such-binary-zz"]}}}\n'


def _tree(root: Path) -> Path:
    """A built-in skills root with: alpha (supporting files, junk to skip, a file symlink),
    beta (always-on), gamma (unavailable), and a directory without SKILL.md."""
    alpha = write_skill(root, "alpha", "Alpha body.")
    (alpha / "scripts").mkdir()
    (alpha / "scripts" / "run.py").write_text("print('alpha')\n")
    (alpha / "refs").mkdir()
    (alpha / "refs" / "notes.txt").write_bytes(b"\x00\x01binary\xff")
    (alpha / "__pycache__").mkdir()
    (alpha / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"pyc")
    (alpha / "scripts" / "__pycache__").mkdir()
    (alpha / "scripts" / "__pycache__" / "x.txt").write_text("cache")
    (alpha / "stale.pyc").write_bytes(b"stale")
    (alpha / ".hidden").write_text("dot file")
    (alpha / ".git").mkdir()
    (alpha / ".git" / "config").write_text("dot dir")
    (alpha / "refs" / ".dotdir").mkdir()
    (alpha / "refs" / ".dotdir" / "f.txt").write_text("nested dot dir")
    write_skill(root, "beta", "Beta always.", extra_front="always: true\n")
    write_skill(root, "gamma", "Gamma needs a binary.", extra_front=UNAVAILABLE)
    (root / "not-a-skill").mkdir()
    (root / "not-a-skill" / "README.md").write_text("no SKILL.md here")
    return root


def _agent(kmaker, skills_root: Path | None, *, work: Path | None = None, **spec_kw):
    variant = Variant(builtin_skills_dir=skills_root) if skills_root is not None else None
    kernel = kmaker(FakeProvider(default="ok"), variant=variant, work=work)
    return kernel.agent(AgentSpec(name=spec_kw.pop("name", "k1"), **spec_kw))


def _skills(agent) -> str:
    return agent.fingerprint().components["skills"]


@pytest.mark.fr("FR-001")
def test_components_have_exactly_five_keys_and_digest_formula(kmaker, tmp_path):
    agent = _agent(kmaker, _tree(tmp_path / "bskills"))
    fp = agent.fingerprint()
    assert isinstance(fp, Fingerprint)
    assert set(fp.components) == FIVE
    assert all(isinstance(v, str) and len(v) == 64 for v in fp.components.values())
    assert fp.digest == fingerprint_digest(dict(fp.components))


@pytest.mark.fr("FR-002", "FR-003", "FR-004")
def test_exact_recomputation_builtin_workspace_inline(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    work = tmp_path / "work"
    delta = write_skill(work / "skills", "delta", "Workspace delta.")
    (delta / "data.json").write_text('{"k": 1}\n')
    outside = tmp_path / "outside.txt"
    outside.write_text("linked target bytes\n")
    os.symlink(outside, root / "alpha" / "refs" / "link.txt")
    inline = {"name": "inl", "description": "an inline skill", "content": "Inline body ü",
              "metadata": {"always": False, "tag": "x"}}
    agent = _agent(kmaker, root, work=work, inline_skills=[inline])
    records = (
        file_records("alpha", root / "alpha")
        + file_records("beta", root / "beta")
        + file_records("gamma", root / "gamma")
        + file_records("delta", delta)
        + [inline_record("inl", inline["description"], inline["content"], inline["metadata"])]
    )
    keys = {k for k, _ in records}
    assert "file:alpha/refs/link.txt" in keys and "file:alpha/scripts/run.py" in keys
    assert not any("__pycache__" in k or "/." in k or k.endswith(".pyc") for k in keys)
    assert _skills(agent) == skills_component(records)


@pytest.mark.fr("FR-003")
def test_dangling_symlink_is_not_a_counted_file(kmaker, tmp_path):
    """A symlink that does not resolve to a regular file is not a record (and not an error)."""
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    before = _skills(agent)
    os.symlink(tmp_path / "missing-target", root / "alpha" / "dangling.txt")
    assert _skills(agent) == before


@pytest.mark.fr("FR-004")
def test_empty_effective_skill_set_is_sha_of_empty_bytes(kmaker, tmp_path):
    root = tmp_path / "empty-skills"
    root.mkdir()
    agent = _agent(kmaker, root)
    assert _skills(agent) == EMPTY_SHA


@pytest.mark.fr("FR-002", "FR-003")
def test_always_on_and_unavailable_skills_are_members(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    before = _skills(agent)
    (root / "beta" / "extra.md").write_text("always-on supporting file")
    after_beta = _skills(agent)
    assert after_beta != before
    (root / "gamma" / "SKILL.md").write_text(
        (root / "gamma" / "SKILL.md").read_text() + "more\n"
    )
    assert _skills(agent) != after_beta


@pytest.mark.fr("FR-002", "FR-003", "FR-005", "FR-006", "SC-002")
def test_one_byte_in_skill_body_changes_only_skills_and_digest(kmaker, tmp_path, sink):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    fp1 = agent.fingerprint()
    path = root / "alpha" / "SKILL.md"
    data = bytearray(path.read_bytes())
    i = data.index(b"Alpha body.")
    data[i] = ord("a")  # one byte, in the body (not the description)
    path.write_bytes(bytes(data))
    fp2 = agent.fingerprint()  # same agent: read at call time
    assert fp2.components["skills"] != fp1.components["skills"]
    assert fp2.digest != fp1.digest
    for name in FIVE - {"skills"}:
        assert fp2.components[name] == fp1.components[name], name


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("change", ["modify", "add", "remove", "add_nested"])
def test_counted_file_changes_change_component(kmaker, tmp_path, change):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    before = _skills(agent)
    alpha = root / "alpha"
    if change == "modify":
        (alpha / "scripts" / "run.py").write_text("print('ALPHA')\n")
    elif change == "add":
        (alpha / "new.txt").write_text("new")
    elif change == "remove":
        (alpha / "refs" / "notes.txt").unlink()
    else:
        (alpha / "refs" / "deep").mkdir()
        (alpha / "refs" / "deep" / "x.md").write_text("deep")
    assert _skills(agent) != before


@pytest.mark.fr("FR-005", "FR-003")
@pytest.mark.parametrize("where", [
    "__pycache__/run.cpython-311.pyc", "scripts/__pycache__/x.txt", "stale.pyc", ".hidden",
    ".git/config", "refs/.dotdir/f.txt", "new.pyc", ".newdot", "__pycache__/new.txt",
    "refs/.newdotdir/a.txt",
])
def test_skipped_paths_do_not_change_component(kmaker, tmp_path, where):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    before = _skills(agent)
    target = root / "alpha" / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"changed " + where.encode())
    assert _skills(agent) == before


@pytest.mark.fr("FR-005", "SC-002")
def test_tree_location_does_not_matter(kmaker, tmp_path):
    src = _tree(tmp_path / "src-skills")
    a_root = tmp_path / "copy-a" / "nested" / "skills-root"
    b_root = tmp_path / "copy-b"
    shutil.copytree(src, a_root)
    shutil.copytree(src, b_root)
    os.utime(b_root / "alpha" / "SKILL.md", (1_000_000, 1_000_000))  # mtime differs
    fa = _agent(kmaker, a_root, work=tmp_path / "wa").fingerprint()
    fb = _agent(kmaker, b_root, work=tmp_path / "wb").fingerprint()
    assert fa.components["skills"] == fb.components["skills"]
    assert fa.digest == fb.digest
    assert fa.components["skills"] == skills_component(
        file_records("alpha", a_root / "alpha") + file_records("beta", a_root / "beta")
        + file_records("gamma", a_root / "gamma")
    )


@pytest.mark.fr("FR-005")
def test_mtime_only_change_is_ignored(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root)
    before = _skills(agent)
    for p in (root / "alpha").rglob("*"):
        if p.is_file() and not p.is_symlink():
            os.utime(p, (2_000_000, 2_000_000))
    assert _skills(agent) == before


@pytest.mark.fr("FR-002", "FR-005")
def test_skills_exclude_removes_member(kmaker, tmp_path):
    """US1 scenario 3."""
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root, skills_exclude=("alpha",))
    before = _skills(agent)
    assert before == skills_component(
        file_records("beta", root / "beta") + file_records("gamma", root / "gamma")
    )
    (root / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\nnew\n")
    (root / "alpha" / "scripts" / "run.py").write_text("changed")
    assert _skills(agent) == before


@pytest.mark.fr("FR-002")
def test_skills_include_restricts_members(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    agent = _agent(kmaker, root, skills_include=("beta",))
    assert _skills(agent) == skills_component(file_records("beta", root / "beta"))
    (root / "alpha" / "scripts" / "run.py").write_text("changed")
    (root / "gamma" / "SKILL.md").write_text("---\nname: gamma\ndescription: g\n---\nx\n")
    assert _skills(agent) == skills_component(file_records("beta", root / "beta"))


@pytest.mark.fr("FR-002", "FR-005")
def test_shadowed_builtin_is_not_a_member(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    work = tmp_path / "work"
    ws_alpha = write_skill(work / "skills", "alpha", "Workspace alpha wins.")
    agent = _agent(kmaker, root, work=work)
    expected = skills_component(
        file_records("alpha", ws_alpha) + file_records("beta", root / "beta")
        + file_records("gamma", root / "gamma")
    )
    assert _skills(agent) == expected
    (root / "alpha" / "scripts" / "run.py").write_text("builtin alpha changed")
    assert _skills(agent) == expected
    (ws_alpha / "SKILL.md").write_text("---\nname: alpha\ndescription: w\n---\nchanged\n")
    assert _skills(agent) != expected


@pytest.mark.fr("FR-002", "FR-003")
def test_inline_skill_shadows_file_skill(kmaker, tmp_path):
    root = _tree(tmp_path / "bskills")
    inline = {"name": "alpha", "description": "inline alpha", "content": "Inline alpha.",
              "metadata": {}}
    agent = _agent(kmaker, root, inline_skills=[inline])
    expected = skills_component(
        [inline_record("alpha", "inline alpha", "Inline alpha.", {})]
        + file_records("beta", root / "beta") + file_records("gamma", root / "gamma")
    )
    assert _skills(agent) == expected
    (root / "alpha" / "scripts" / "run.py").write_text("shadowed change")
    assert _skills(agent) == expected


@pytest.mark.fr("FR-003")
def test_inline_skill_values_enter_the_record(kmaker, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    base = {"name": "inl", "description": "d", "content": "c", "metadata": {"a": 1}}
    seen = set()
    for variant in (base, {**base, "content": "c2"}, {**base, "description": "d2"},
                    {**base, "metadata": {"a": 2}}):
        agent = _agent(kmaker, root, name=f"a{len(seen)}", inline_skills=[variant])
        value = _skills(agent)
        assert value == skills_component([inline_record(
            "inl", variant["description"], variant["content"], variant["metadata"])])
        seen.add(value)
    assert len(seen) == 4


@pytest.mark.fr("FR-002")
def test_bundled_skills_count_without_variant_dir(kmaker, tmp_path):
    """Without builtin_skills_dir the bundled skills are members: the component is not the
    empty-set hash, and a workspace skill adds to it."""
    agent = _agent(kmaker, None)
    bundled = _skills(agent)
    assert bundled != EMPTY_SHA
    write_skill(tmp_path / "work" / "skills", "zz-h006", "Workspace skill.")
    assert _skills(agent) != bundled


@pytest.mark.fr("FR-006")
def test_unreadable_counted_file_raises_oserror(kmaker, tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    root = _tree(tmp_path / "bskills")
    secret = root / "alpha" / "refs" / "locked.txt"
    secret.write_text("locked")
    agent = _agent(kmaker, root)
    secret.chmod(0)
    try:
        with pytest.raises(OSError):
            agent.fingerprint()
    finally:
        secret.chmod(0o600)


@pytest.mark.fr("FR-006", "FR-003")
def test_unreadable_skipped_file_is_not_read(kmaker, tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    root = _tree(tmp_path / "bskills")
    locked = root / "alpha" / "__pycache__" / "locked.pyc"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        agent = _agent(kmaker, root)
        assert _skills(agent) == skills_component(
            file_records("alpha", root / "alpha") + file_records("beta", root / "beta")
            + file_records("gamma", root / "gamma")
        )
    finally:
        locked.chmod(0o600)


@pytest.mark.fr("FR-006", "FR-038")
def test_fingerprint_emits_no_skill_read(kmaker, tmp_path, sink):
    root = _tree(tmp_path / "bskills")
    write_skill(tmp_path / "work" / "skills", "delta")
    agent = _agent(kmaker, root)
    agent.fingerprint()
    agent.fingerprint()
    assert ev(sink, "skill.read") == []
