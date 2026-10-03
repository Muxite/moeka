"""SC-002: with enforcement on, flipping one byte of any bundle file, renaming the dir, editing
``io`` or revoking the cert removes the capability from the prompt summary and the tool registry
on the next load (a fresh kernel over the same workspace)."""

from __future__ import annotations

import os

import pytest
import yaml
from _h012 import (
    Signer,
    agent_of,
    caps_for,
    certify,
    flip_byte,
    revoke,
    summary_names,
    system_prompt_of,
    tool_names,
    trust_dir,
    write_script_tool,
    write_skill,
)

SKILL = "h012-sc"
TOOL = "h012-sct"


def _fixture(tmp_path):
    alpha = Signer("alpha")
    work = tmp_path / "work"
    t = trust_dir(tmp_path / "trust", alpha)
    sb = write_skill(work, SKILL, extra_files={"ref/notes.txt": "MARK notes\n"})
    tb = write_script_tool(work, TOOL)
    ids = {"skill": certify(sb, alpha), "tool": certify(tb, alpha)}
    return work, t, sb, tb, ids


def _present(kmaker, work, t):
    kernel, fake = kmaker(work=work, caps=caps_for(True, t), script_enabled=True)
    agent = agent_of(kernel)
    agent.run_sync("hello")
    return summary_names(system_prompt_of(fake)), tool_names(agent)


@pytest.mark.fr("SC-002", "FR-006")
def test_baseline_both_present(kmaker, tmp_path):
    work, t, *_ = _fixture(tmp_path)
    skills, tools = _present(kmaker, work, t)
    assert SKILL in skills
    assert f"script_{TOOL}" in tools


def _rewrite_skill_io(sb):
    text = (sb / "SKILL.md").read_text(encoding="utf-8")
    (sb / "SKILL.md").write_text(text.replace("read-workspace", "write-workspace"),
                                 encoding="utf-8")


def _rewrite_tool_io(tb):
    m = yaml.safe_load((tb / "tool.yaml").read_text(encoding="utf-8"))
    m["io"]["effects"] = ["exec"]
    (tb / "tool.yaml").write_text(yaml.safe_dump(m, sort_keys=False), encoding="utf-8")


SKILL_MUTATIONS = {
    "flip-skill-md": lambda sb, t, ids: flip_byte(sb / "SKILL.md"),
    "flip-nested-file": lambda sb, t, ids: flip_byte(sb / "ref" / "notes.txt"),
    "add-file": lambda sb, t, ids: (sb / "extra.txt").write_text("x", encoding="utf-8"),
    "rename-dir": lambda sb, t, ids: os.rename(sb, sb.parent / f"{SKILL}-moved"),
    "edit-io": lambda sb, t, ids: _rewrite_skill_io(sb),
    "revoke": lambda sb, t, ids: revoke(t, ids["skill"]),
}

TOOL_MUTATIONS = {
    "flip-tool-yaml": lambda tb, t, ids: flip_byte(tb / "tool.yaml", b"Script"),
    "flip-entry": lambda tb, t, ids: flip_byte(tb / "run"),
    "add-file": lambda tb, t, ids: (tb / "extra.txt").write_text("x", encoding="utf-8"),
    "rename-dir": lambda tb, t, ids: os.rename(tb, tb.parent / f"{TOOL}-moved"),
    "edit-io": lambda tb, t, ids: _rewrite_tool_io(tb),
    "revoke": lambda tb, t, ids: revoke(t, ids["tool"]),
}


@pytest.mark.fr("SC-002", "FR-006")
@pytest.mark.parametrize("mutation", sorted(SKILL_MUTATIONS))
def test_skill_removed_after_mutation(kmaker, tmp_path, mutation):
    work, t, sb, _tb, ids = _fixture(tmp_path)
    skills, _ = _present(kmaker, work, t)
    assert SKILL in skills, "fixture: certified skill not listed before the mutation"
    SKILL_MUTATIONS[mutation](sb, t, ids)
    skills, tools = _present(kmaker, work, t)
    assert SKILL not in skills and f"{SKILL}-moved" not in skills
    assert f"script_{TOOL}" in tools, "the untouched script tool must stay"


@pytest.mark.fr("SC-002", "FR-006")
@pytest.mark.parametrize("mutation", sorted(TOOL_MUTATIONS))
def test_script_tool_removed_after_mutation(kmaker, tmp_path, mutation):
    work, t, _sb, tb, ids = _fixture(tmp_path)
    _, tools = _present(kmaker, work, t)
    assert f"script_{TOOL}" in tools, "fixture: certified script tool not registered"
    TOOL_MUTATIONS[mutation](tb, t, ids)
    skills, tools = _present(kmaker, work, t)
    assert f"script_{TOOL}" not in tools and f"script_{TOOL}-moved" not in tools
    assert SKILL in skills, "the untouched skill must stay"
