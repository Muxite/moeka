"""Workspace skill validation in ``SkillsLoader`` (spec 011 FR-003)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loguru import logger

from nanobot.agent.skills import SkillsLoader


def _write(root: Path, name: str, front: str, body: str = "Body.") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def warnings():
    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="WARNING", format="{message}")
    yield messages
    logger.remove(handler)


@pytest.fixture
def dirs(tmp_path):
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return workspace, workspace / "skills", builtin


def _names(loader: SkillsLoader) -> set[str]:
    return {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}


def test_invalid_workspace_skills_are_not_listed_or_loaded(dirs, warnings):
    workspace, skills, builtin = dirs
    _write(skills, "good", "name: good\ndescription: fine\nio:\n  inputs: [file:pdf]\n")
    _write(skills, "mismatch", "name: other\ndescription: wrong name\n")
    _write(skills, "no-desc", "name: no-desc\n")
    _write(skills, "bad-io", "name: bad-io\ndescription: d\nio:\n  inputs: [file:docx]\n")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    assert _names(loader) == {"good"}
    assert loader.load_skill("mismatch") is None
    assert loader.load_skill("bad-io") is None
    assert "mismatch" not in loader.build_skills_summary()
    assert loader.get_explicitly_invoked_skills("use $bad-io") == []


def test_one_warning_per_invalid_skill_naming_it_and_the_first_problem(dirs, warnings):
    workspace, skills, builtin = dirs
    _write(skills, "bad-io", "name: bad-io\ndescription: d\nio:\n  effects: [teleport]\n")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    for _ in range(3):
        loader.list_skills()
        loader.build_skills_summary()
    hits = [m for m in warnings if "bad-io" in m]
    assert len(hits) == 1
    assert "io.effects" in hits[0] and "teleport" in hits[0]


def test_builtin_skills_are_not_validated_at_runtime(dirs):
    workspace, _, builtin = dirs
    _write(builtin, "loose", "description: no name field\n")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert "loose" in _names(loader)


def test_fixing_a_skill_makes_it_visible_again(dirs, warnings):
    workspace, skills, builtin = dirs
    path = _write(skills, "fixme", "name: fixme\n")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert "fixme" not in _names(loader)
    path.write_text("---\nname: fixme\ndescription: now valid\n---\n\nBody.\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert "fixme" in _names(loader)


def test_invalid_workspace_skill_does_not_shadow_the_builtin(dirs):
    workspace, skills, builtin = dirs
    _write(builtin, "weather", "name: weather\ndescription: built-in weather\n")
    _write(skills, "weather", "name: weather\n")  # invalid: no description
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    [entry] = [e for e in loader.list_skills(filter_unavailable=False) if e["name"] == "weather"]
    assert entry["source"] == "builtin"
