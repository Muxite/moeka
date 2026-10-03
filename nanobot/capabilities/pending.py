"""Pending skills: Dream's staged skill writes and their review (spec 010 FR-001).

With ``dream.skill_writes = "pending"`` (the default), Dream's skill creates and merges land
in ``<workspace>/skills-pending/<name>/`` instead of ``<workspace>/skills/<name>/``. The
owner reviews them with ``nanobot capabilities pending``, then:

- ``accept <name>`` moves the skill into ``skills/`` when ``validate_skill`` reports no
  problem and lint reports no finding (``force_lint`` waives lint, never validation). A
  skill it replaces is kept under ``skills-superseded/<date>/<name>``;
- ``reject <name>`` moves it to ``skills-rejected/<date>/<name>``.

``SkillsLoader`` never lists or loads ``skills-pending/``.

Kernel code: imports no host package.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from nanobot.capabilities.lint import Finding, finding_codes, lint_file
from nanobot.capabilities.manifest import validate_skill

PENDING_DIRNAME = "skills-pending"
REJECTED_DIRNAME = "skills-rejected"
SUPERSEDED_DIRNAME = "skills-superseded"


def skills_dir(workspace: Path) -> Path:
    return workspace / "skills"


def pending_dir(workspace: Path) -> Path:
    return workspace / PENDING_DIRNAME


def rejected_dir(workspace: Path, day: date | None = None) -> Path:
    return workspace / REJECTED_DIRNAME / (day or date.today()).isoformat()


@dataclass(frozen=True)
class PendingSkill:
    """One staged skill with its review results."""

    name: str
    path: Path  # the skill directory under skills-pending/
    problems: list[str] = field(default_factory=list)  # validate_skill
    findings: list[Finding] = field(default_factory=list)  # lint
    replaces: bool = False  # a skill of that name already exists in skills/

    @property
    def lint_codes(self) -> list[str]:
        return finding_codes(self.findings)


class PendingError(Exception):
    """An accept or reject that cannot proceed; the message says why."""


def _review(workspace: Path, skill: Path) -> PendingSkill:
    skill_file = skill / "SKILL.md"
    return PendingSkill(
        name=skill.name,
        path=skill,
        problems=validate_skill(skill_file),
        findings=lint_file(skill_file),
        replaces=(skills_dir(workspace) / skill.name).exists(),
    )


def list_pending(workspace: Path) -> list[PendingSkill]:
    """Every staged skill (a directory holding ``SKILL.md``), sorted by name."""
    root = pending_dir(workspace)
    if not root.is_dir():
        return []
    return [
        _review(workspace, skill)
        for skill in sorted(root.iterdir())
        if skill.is_dir() and not skill.name.startswith(".") and (skill / "SKILL.md").is_file()
    ]


def get_pending(workspace: Path, name: str) -> PendingSkill:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise PendingError(f"invalid skill name {name!r}")
    skill = pending_dir(workspace) / name
    if not (skill / "SKILL.md").is_file():
        raise PendingError(f"no pending skill named {name!r} in {pending_dir(workspace)}")
    return _review(workspace, skill)


def _free_target(base: Path) -> Path:
    if not base.exists():
        return base
    n = 1
    while (candidate := base.with_name(f"{base.name}-{n}")).exists():
        n += 1
    return candidate


def accept_pending(workspace: Path, name: str, *, force_lint: bool = False) -> PendingSkill:
    """Move a valid, lint-clean pending skill into ``skills/``; raise :class:`PendingError`."""
    skill = get_pending(workspace, name)
    if skill.problems:
        raise PendingError(f"{name}: invalid: " + "; ".join(skill.problems))
    if skill.findings and not force_lint:
        raise PendingError(
            f"{name}: lint findings: " + ", ".join(skill.lint_codes)
            + " (review them; --force-lint accepts anyway)"
        )
    target = skills_dir(workspace) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = _free_target(workspace / SUPERSEDED_DIRNAME / date.today().isoformat() / name)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(backup))
    shutil.move(str(skill.path), str(target))
    return skill


def reject_pending(workspace: Path, name: str) -> Path:
    """Move a pending skill to ``skills-rejected/<date>/<name>``; return where it went."""
    skill = get_pending(workspace, name)
    target = _free_target(rejected_dir(workspace) / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill.path), str(target))
    return target


__all__ = [
    "PENDING_DIRNAME",
    "REJECTED_DIRNAME",
    "SUPERSEDED_DIRNAME",
    "PendingError",
    "PendingSkill",
    "accept_pending",
    "get_pending",
    "list_pending",
    "pending_dir",
    "reject_pending",
    "rejected_dir",
    "skills_dir",
]
