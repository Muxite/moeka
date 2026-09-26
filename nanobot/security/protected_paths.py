"""Resolved-path floor for the file tools.

A small set of paths that the file tools (read_file, write_file, edit_file,
apply_patch, list_dir, find_files, grep, image-generation reference images)
must never read or write, whatever ``restrict_to_workspace`` or the
``extra_*_allowed_*`` lists say. The check runs on the path *after*
``Path.resolve()``, so a symlink into a protected location is denied too.

This is an application-level guard on the file tools only. It does not stop
``exec``: a shell command can still reach these paths (see the exec caveat in
docs/core-map/02-tools.md). Real containment needs OS isolation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from nanobot.security.workspace_policy import WORKSPACE_BOUNDARY_NOTE, WorkspaceBoundaryError

PROTECTED_MARKER = "protected internal path (not configurable)"

# Per-process secrets under /proc. ``/proc/self`` and ``/proc/thread-self``
# resolve to ``/proc/<pid>`` and ``/proc/<pid>/task/<tid>``, so the pattern
# matches the resolved form (and the literal form, which is checked too).
# /proc/cpuinfo, /proc/meminfo, /proc/<pid>/status and /proc/<pid>/cmdline
# stay readable.
_PROC_SECRET_RE = re.compile(
    r"^/proc/(?:[^/]+/task/)?[^/]+/(?:environ|mem|maps|root|cwd|exe)(?:/|$)"
)

# READ and WRITE denied: these subdirectories of the instance data dir
# (``get_config_path().parent``). ``sessions`` is the JSONL session store's
# default root (``get_runtime_subdir("sessions")``); the SQLite sessions root
# (``default_sessions_root``) is added per workspace in ``_protected_roots``.
PROTECTED_READ: tuple[str, ...] = ("auth", "plugin-data", "sessions")

# WRITE only denied: internal-state files, matched by their last two path
# components in any workspace (so an unrestricted tool cannot overwrite a
# different workspace's history either). Reading them stays allowed.
PROTECTED_WRITE: tuple[tuple[str, str], ...] = (
    ("memory", "history.jsonl"),
    ("memory", ".dream_cursor"),
    (".nanobot", "workspace-id"),
)


class ProtectedPathError(WorkspaceBoundaryError):
    """Raised when a file tool targets a protected internal path."""


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _protected_roots(data_dir: Path | None, workspace: Path | None) -> list[Path]:
    from nanobot.session.sqlite_store import default_sessions_root

    roots: list[Path] = []
    resolved_data = _safe_resolve(data_dir) if data_dir is not None else None
    if resolved_data is not None:
        roots.extend(resolved_data / name for name in PROTECTED_READ)
        roots.append(default_sessions_root(resolved_data))
    resolved_ws = _safe_resolve(workspace) if workspace is not None else None
    if resolved_ws is not None and resolved_ws != resolved_data:
        roots.append(default_sessions_root(resolved_ws))
    return roots


def _match(candidate: Path, *, write: bool, roots: list[Path]) -> bool:
    posix = candidate.as_posix()
    if _PROC_SECRET_RE.match(posix):
        return True
    if any(_is_within(candidate, root) for root in roots):
        return True
    if write and tuple(candidate.parts[-2:]) in PROTECTED_WRITE:
        return True
    return False


class ProtectedFloor:
    """Precomputed floor for one (data dir, workspace) pair; cheap per path."""

    def __init__(self, *, data_dir: Path | None, workspace: Path | None) -> None:
        self._roots = _protected_roots(data_dir, workspace)

    def matches(self, path: Path, *, write: bool, resolve: bool = True) -> bool:
        """True when *path* (as given, and after ``resolve()``) is protected."""
        try:
            logical = Path(os.path.abspath(Path(path).expanduser()))
        except (OSError, ValueError):
            logical = None
        if logical is not None and _match(logical, write=write, roots=self._roots):
            return True
        if not resolve:
            return False
        resolved = _safe_resolve(Path(path))
        return resolved is not None and _match(resolved, write=write, roots=self._roots)

    def reason(self, path: Path, *, write: bool) -> str | None:
        if not self.matches(path, write=write):
            return None
        verb = "writing" if write else "reading"
        return (
            f"Path {path} is a {PROTECTED_MARKER}; {verb} it through the file "
            "tools is always denied" + WORKSPACE_BOUNDARY_NOTE
        )


def protected_reason(
    path: Path,
    *,
    write: bool,
    data_dir: Path | None,
    workspace: Path | None,
) -> str | None:
    """Return a denial message if *path* is protected, else ``None``.

    *path* is checked both as given (made absolute, not following symlinks)
    and after ``resolve()``; either one matching denies. The message always
    contains ``PROTECTED_MARKER``.
    """
    return ProtectedFloor(data_dir=data_dir, workspace=workspace).reason(path, write=write)


def default_data_dir() -> Path | None:
    """The instance data dir (``get_data_dir()`` without creating it)."""
    try:
        from nanobot.config.paths import get_config_path

        return get_config_path().expanduser().resolve(strict=False).parent
    except Exception:
        return None


def check_protected(
    path: Path,
    *,
    write: bool,
    workspace: Path | None,
    data_dir: Path | None = None,
) -> None:
    """Raise ``ProtectedPathError`` when *path* is protected."""
    reason = protected_reason(
        path,
        write=write,
        data_dir=data_dir if data_dir is not None else default_data_dir(),
        workspace=workspace,
    )
    if reason is not None:
        raise ProtectedPathError(reason)
