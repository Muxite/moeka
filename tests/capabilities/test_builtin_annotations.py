"""Built-in skills and tools carry truthful ``io`` blocks (spec 011 FR-003, FR-014)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, parse_skill_metadata
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.exec_session import ExecSessionTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.search import FindFilesTool, GrepTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.capabilities.manifest import parse_io, validate_skill

BUILTIN_SKILLS = sorted(p.parent for p in BUILTIN_SKILLS_DIR.glob("*/SKILL.md"))


def test_there_are_builtin_skills():
    assert len(BUILTIN_SKILLS) >= 10


@pytest.mark.parametrize("skill_dir", BUILTIN_SKILLS, ids=lambda p: p.name)
def test_builtin_skill_is_valid_and_typed(skill_dir: Path):
    assert validate_skill(skill_dir) == []
    metadata = parse_skill_metadata((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    assert metadata is not None and "io" in metadata, f"{skill_dir.name} has no io block"
    assert parse_io(metadata["io"]) is not None


def test_tool_io_defaults_to_untyped():
    assert Tool.io is None


@pytest.mark.parametrize(("tool", "effect"), [
    (ExecTool, "exec"),
    (ExecSessionTool, "exec"),
    (WebSearchTool, "network"),
    (WebFetchTool, "network"),
    (ReadFileTool, "read-workspace"),
    (ListDirTool, "read-workspace"),
    (FindFilesTool, "read-workspace"),
    (GrepTool, "read-workspace"),
    (WriteFileTool, "write-workspace"),
    (EditFileTool, "write-workspace"),
    (ApplyPatchTool, "write-workspace"),
])
def test_builtin_tools_declare_truthful_effects(tool, effect):
    spec = parse_io(tool.io)
    assert spec is not None and effect in spec.effects


def test_read_only_tools_do_not_claim_writes():
    for tool in (ReadFileTool, ListDirTool, FindFilesTool, GrepTool):
        assert "write-workspace" not in parse_io(tool.io).effects
