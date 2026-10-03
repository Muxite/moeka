"""Dream's file tools with skill writes staged in ``skills-pending/`` (spec 010 FR-001).

Dream addresses skills as ``skills/<name>/...`` (its prompt says so). In ``"pending"``
mode these tools map the target:

- a write (``write_file``, ``edit_file``, ``apply_patch``) to ``skills/<name>/<rest>`` lands in
  ``skills-pending/<name>/<rest>``. When ``skills/<name>/`` exists and has no pending copy
  yet, it is copied first, so a merge edits the copy and ``skills/`` is never touched;
- a read of ``skills/<name>/<rest>`` returns the pending copy when one exists, so Dream
  verifies what it wrote. Without one it reads ``skills/`` as before.

Every other path (``SOUL.md``, ``USER.md``, ``memory/MEMORY.md``) is unchanged.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool


class _PendingSkillsMap:
    """Mixin for an ``_FsTool``: remap resolved ``skills/`` paths to ``skills-pending/``."""

    _skills_root: Path
    _pending_root: Path

    def _init_pending(self, skills_root: Path, pending_root: Path) -> None:
        self._skills_root = skills_root.expanduser().resolve(strict=False)
        self._pending_root = pending_root.expanduser().resolve(strict=False)

    def _skill_rel(self, resolved: Path) -> Path | None:
        try:
            rel = resolved.relative_to(self._skills_root)
        except ValueError:
            return None
        return rel if rel.parts else None

    def _resolve_write(self, path: str) -> Path:
        resolved: Path = super()._resolve_write(path)  # type: ignore[misc]
        rel = self._skill_rel(resolved)
        if rel is None:
            return resolved
        name = rel.parts[0]
        source = self._skills_root / name
        staged = self._pending_root / name
        if source.is_dir() and not staged.exists():
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staged, symlinks=True)
        target = self._pending_root / rel
        return self._check_floor(target, write=True)  # type: ignore[attr-defined]

    def _resolve_read(self, path: str) -> Path:
        resolved: Path = super()._resolve_read(path)  # type: ignore[misc]
        rel = self._skill_rel(resolved)
        if rel is None:
            return resolved
        staged = self._pending_root / rel
        return staged if staged.exists() else resolved


def _pending_tool(base: type) -> type:
    class Tool(_PendingSkillsMap, base):  # type: ignore[misc, valid-type]
        def __init__(self, *, skills_root: Path, pending_root: Path, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._init_pending(skills_root, pending_root)

    Tool.__name__ = Tool.__qualname__ = f"Pending{base.__name__}"
    return Tool


PendingReadFileTool = _pending_tool(ReadFileTool)
PendingEditFileTool = _pending_tool(EditFileTool)
PendingApplyPatchTool = _pending_tool(ApplyPatchTool)
PendingWriteFileTool = _pending_tool(WriteFileTool)

__all__ = [
    "PendingApplyPatchTool",
    "PendingEditFileTool",
    "PendingReadFileTool",
    "PendingWriteFileTool",
]
