"""Install portable workspace skill seeds without replacing local edits."""

from __future__ import annotations

import shutil
from pathlib import Path

_PORTABLE_SKILL_NAMES = (
    "test-runner", "python-work", "structured-data", "notes",
    "document-processing", "media-processing",
)


def seed_portable_skills(workspace: Path) -> list[str]:
    """Copy missing bundled skill files into ``workspace/skills``.

    Existing files are always preserved. This makes the operation safe to run on
    every instance startup while still making newly added seeds available.
    """
    source_root = Path(__file__).resolve().parents[1] / "skills" / "seeds"
    target_root = workspace / "skills"
    installed: list[str] = []
    if not source_root.is_dir():
        return installed
    for name in _PORTABLE_SKILL_NAMES:
        source_skill = source_root / name
        if not source_skill.is_dir():
            continue
        target_skill = target_root / source_skill.name
        copied = False
        for source_file in source_skill.rglob("*"):
            if not source_file.is_file():
                continue
            target_file = target_skill / source_file.relative_to(source_skill)
            if target_file.exists():
                continue
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            copied = True
        if copied:
            installed.append(source_skill.name)
    return installed
