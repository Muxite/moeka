"""Resolved-path floor for the file tools.

A small set of paths that the file tools (read_file, write_file, edit_file,
apply_patch, list_dir, find_files, grep, image-generation reference images)
must never read or write, whatever ``restrict_to_workspace`` or the
``extra_*_allowed_*`` lists say. The check runs on the path *after*
``Path.resolve()``, so a symlink into a protected location is denied too.

This is an application-level guard on the file tools only. It does not stop
``exec``: a shell command can still reach these paths (see the exec caveat in
docs/core-map/02-tools.md). Real containment needs OS isolation.

Several instances on one machine (spec 005): every ``keys.env`` (any directory), the
``.env`` directly inside an instance root, every OTHER instance's root and its
``<root>-sessions`` sibling are denied for read and write; the lock, PID and socket
files of the own instance are write-denied. This still covers the file tools only:
``exec`` can reach all of these, so real separation between instances needs separate
UIDs, the bwrap sandbox, or containers (docs/multiple-instances.md).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.security.workspace_policy import WORKSPACE_BOUNDARY_NOTE, WorkspaceBoundaryError

if TYPE_CHECKING:
    from nanobot.kernel.env import Paths

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

# WRITE only denied: the instance config file itself (``get_config_path()``,
# plus its resolved symlink target). Reading stays allowed: the model may
# inspect non-secret config (secrets are ``${VAR}`` references, not values).
# The loader's atomic write uses a random ``.config.json.<uuid>.tmp`` name that
# exists only inside ``save_config``; no ``.bak`` is ever created, so there are
# no sibling names to protect. Code-level writers (``save_config``, the CLI)
# do not go through the file tools and are unaffected.
#
# WRITE only denied: internal-state files, matched by their last two path
# components in any workspace (so an unrestricted tool cannot overwrite a
# different workspace's history either). Reading them stays allowed.
PROTECTED_WRITE: tuple[tuple[str, str], ...] = (
    ("memory", "history.jsonl"),
    ("memory", ".dream_cursor"),
    (".nanobot", "workspace-id"),
)

# READ and WRITE denied wherever they are: secrets files by their last component.
PROTECTED_BASENAMES: frozenset[str] = frozenset({"keys.env"})

# WRITE denied directly inside an instance root / state dir (spec 005 FR-040): the
# instance lock and holder record, the gateway PID file and lock, and the run dir
# (WebSocket socket). Reading stays allowed.
INSTANCE_WRITE_PROTECTED: tuple[str, ...] = (
    ".instance.lock", ".instance.json", "moeka.pid", "gateway.lock", "run",
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


DataDirs = Path | Sequence[Path] | None


def _protected_roots(data_dir: DataDirs, workspace: Path | None) -> list[Path]:
    from nanobot.session.sqlite_store import default_sessions_root

    if data_dir is None:
        bases: Sequence[Path] = ()
    elif isinstance(data_dir, Path):
        bases = (data_dir,)
    else:
        bases = data_dir
    resolved_bases: list[Path] = []
    for base in bases:
        resolved = _safe_resolve(base)
        if resolved is not None and resolved not in resolved_bases:
            resolved_bases.append(resolved)

    roots: list[Path] = []
    for resolved_data in resolved_bases:
        roots.extend(resolved_data / name for name in PROTECTED_READ)
        # The usage ledger: an agent that can rewrite it can forge what its own calls cost.
        roots.extend(resolved_data / name for name in _usage_store_filenames())
        roots.append(default_sessions_root(resolved_data))
    resolved_ws = _safe_resolve(workspace) if workspace is not None else None
    if resolved_ws is not None and resolved_ws not in resolved_bases:
        roots.append(default_sessions_root(resolved_ws))
    if resolved_ws is not None:
        # Legacy hosts without Paths: the workspace is the state home, which holds the
        # host-owned kernel state files (plugin registry, fact store).
        roots.extend(resolved_ws / name for name in _kernel_state_filenames())
    return roots


def _usage_store_filenames() -> tuple[str, ...]:
    """The usage database (``<data_dir>/llm_usage.sqlite3``) and its SQLite sidecars."""
    from nanobot.llm_usage import USAGE_DB_FILENAME

    return tuple(USAGE_DB_FILENAME + suffix for suffix in ("", "-wal", "-shm", "-journal"))


def _kernel_state_filenames() -> tuple[str, ...]:
    """Host-owned kernel state files kept directly in ``state_dir``.

    The kernel plugin registry, and the fact store and artifact store databases
    with their SQLite sidecars (``-wal``/``-shm``/``-journal``).
    """
    from nanobot.kernel.artifacts import ARTIFACTS_DB_FILENAME
    from nanobot.kernel.facts import FACTS_DB_FILENAME
    from nanobot.kernel.registry import REGISTRY_FILENAME

    return (
        REGISTRY_FILENAME,
        *(
            db + suffix
            for db in (FACTS_DB_FILENAME, ARTIFACTS_DB_FILENAME)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
    )


def _match(
    candidate: Path,
    *,
    write: bool,
    roots: list[Path],
    config_files: Sequence[Path] = (),
    write_roots: Sequence[Path] = (),
) -> bool:
    posix = candidate.as_posix()
    if _PROC_SECRET_RE.match(posix):
        return True
    if candidate.name in PROTECTED_BASENAMES:
        return True
    if any(_is_within(candidate, root) for root in roots):
        return True
    if write and tuple(candidate.parts[-2:]) in PROTECTED_WRITE:
        return True
    if write and candidate in config_files:
        return True
    if write and any(_is_within(candidate, root) for root in write_roots):
        return True
    return False


def _instance_roots_extras(
    own_roots: Sequence[Path], other_roots: Sequence[Path],
) -> tuple[list[Path], list[Path]]:
    """``(read+write roots, write-only roots)`` for the instance-aware floor (spec 005).

    *own_roots* are this instance's root-like directories (state dir, data dir, the flat
    layout's workspace): their ``.env`` is denied, their lock/PID/run files write-denied.
    *other_roots* are other instances' roots: each root and its ``-sessions`` sibling are
    denied entirely.
    """
    from nanobot.session.sqlite_store import default_sessions_root

    deny: list[Path] = []
    write_only: list[Path] = []
    for root in own_roots:
        resolved = _safe_resolve(root)
        if resolved is None:
            continue
        deny.append(resolved / ".env")
        write_only.extend(resolved / name for name in INSTANCE_WRITE_PROTECTED)
    for root in other_roots:
        for candidate in (Path(root), _safe_resolve(Path(root))):
            if candidate is None:
                continue
            candidate = Path(os.path.abspath(candidate))
            for item in (candidate, default_sessions_root(candidate)):
                if item not in deny:
                    deny.append(item)
    return deny, write_only


class ProtectedFloor:
    """Precomputed floor for one (data dir, workspace) pair; cheap per path."""

    def __init__(
        self,
        *,
        data_dir: DataDirs,
        workspace: Path | None,
        config_files: Sequence[Path] = (),
        other_instance_roots: Sequence[Path] = (),
        instance_roots: Sequence[Path] | None = None,
    ) -> None:
        """*other_instance_roots*: other instances' roots, denied with their sessions
        siblings (hosts pass them; the kernel never discovers). *instance_roots*: this
        instance's root-like dirs (default: the data dirs and *workspace*)."""
        self._roots = _protected_roots(data_dir, workspace)
        if instance_roots is None:
            if data_dir is None:
                bases: list[Path] = []
            elif isinstance(data_dir, Path):
                bases = [data_dir]
            else:
                bases = list(data_dir)
            instance_roots = [*bases, *([workspace] if workspace is not None else [])]
        deny, write_only = _instance_roots_extras(instance_roots, other_instance_roots)
        for root in deny:
            if root not in self._roots:
                self._roots.append(root)
        self._write_roots: list[Path] = list(dict.fromkeys(write_only))
        self._config_files: list[Path] = []
        for cfg in config_files:
            resolved = _safe_resolve(cfg)
            for candidate in (Path(os.path.abspath(cfg.expanduser())), resolved):
                if candidate is not None and candidate not in self._config_files:
                    self._config_files.append(candidate)

    @classmethod
    def from_paths(
        cls,
        paths: Paths,
        *,
        extra_data_dirs: Sequence[Path] = (),
        config_files: Sequence[Path] = (),
        other_instance_roots: Sequence[Path] = (),
    ) -> ProtectedFloor:
        """Floor for a host's ``Paths`` (the agent's file tools run in ``work_dir``).

        Covers ``auth/``, ``plugin-data/`` and ``sessions/`` under ``data_dir``, the
        ``sessions_root``, and the workspace-internal write-only files. When
        ``work_dir`` and ``state_dir`` are separated, all of ``state_dir`` is
        denied. In the legacy flat layout ``state_dir`` is the workspace, so only
        the named subtrees are protected there, plus the host-owned kernel state
        files (plugin registry ``nanobot.kernel.registry.REGISTRY_FILENAME``, fact store
        ``nanobot.kernel.facts.FACTS_DB_FILENAME`` and its sidecars) in either layout. *extra_data_dirs*/*config_files*
        carry the legacy ambient roots (``default_data_dirs()``/``default_config_files()``).
        """
        bases: list[Path] = [paths.data_dir, *extra_data_dirs]
        # The instance's own root-like dirs: the state dir and data dirs, plus the work
        # dir in the flat layout (where it is the instance root).
        own: list[Path] = [paths.state_dir, *bases]
        if paths.overlaps:
            own.append(paths.work_dir)
        floor = cls(
            data_dir=bases, workspace=paths.work_dir, config_files=config_files,
            other_instance_roots=other_instance_roots, instance_roots=own,
        )
        extra = [paths.sessions_root]
        extra.extend(paths.state_dir / name for name in _kernel_state_filenames())
        if not paths.overlaps:
            extra.append(paths.state_dir)
        for root in extra:
            resolved = _safe_resolve(root)
            if resolved is not None and resolved not in floor._roots:
                floor._roots.append(resolved)
        return floor

    def matches(self, path: Path, *, write: bool, resolve: bool = True) -> bool:
        """True when *path* (as given, and after ``resolve()``) is protected."""
        try:
            logical = Path(os.path.abspath(Path(path).expanduser()))
        except (OSError, ValueError):
            logical = None
        if logical is not None and _match(
            logical, write=write, roots=self._roots, config_files=self._config_files,
            write_roots=self._write_roots,
        ):
            return True
        if not resolve:
            return False
        resolved = _safe_resolve(Path(path))
        return resolved is not None and _match(
            resolved, write=write, roots=self._roots, config_files=self._config_files,
            write_roots=self._write_roots,
        )

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
    data_dir: DataDirs,
    workspace: Path | None,
) -> str | None:
    """Return a denial message if *path* is protected, else ``None``.

    *path* is checked both as given (made absolute, not following symlinks)
    and after ``resolve()``; either one matching denies. The message always
    contains ``PROTECTED_MARKER``.
    """
    return ProtectedFloor(data_dir=data_dir, workspace=workspace).reason(path, write=write)


def default_data_dirs() -> list[Path]:
    """The instance data dir(s), computed without creating anything.

    Two conventions coexist when ``config.json`` is a symlink: ``get_data_dir()``
    (auth stores, sessions) uses the directory holding the LINK, while
    ``_plugin_data_dir`` (plugin-data) uses the directory holding the resolved
    TARGET. Both are returned (deduplicated) so the floor covers either.
    """
    try:
        from nanobot.kernel.legacy import legacy_config_path

        config = legacy_config_path().expanduser()
    except Exception:
        return []
    link_base = _safe_resolve(config.parent)  # get_data_dir() convention
    target = _safe_resolve(config)
    target_base = target.parent if target is not None else None  # plugin-data convention
    dirs: list[Path] = []
    for base in (link_base, target_base):
        if base is not None and base not in dirs:
            dirs.append(base)
    return dirs


def default_config_files() -> list[Path]:
    """The configured config file path and its resolved symlink target.

    Computed without creating anything; empty when the path cannot be read.
    """
    try:
        from nanobot.kernel.legacy import legacy_config_path

        config = legacy_config_path().expanduser()
    except Exception:
        return []
    files = [Path(os.path.abspath(config))]
    target = _safe_resolve(config)
    if target is not None and target not in files:
        files.append(target)
    return files


def default_other_instance_roots(
    workspace: Path | None, data_dirs: DataDirs = None,
) -> list[Path]:
    """Legacy hosts: the other discovered instances (``$HOME`` + registry), never raising.

    "Other" means a root that neither equals nor contains the agent's own work dir
    (*workspace*, else the first data dir).
    """
    own = workspace
    if own is None:
        if isinstance(data_dirs, Path):
            own = data_dirs
        elif data_dirs:
            own = list(data_dirs)[0]
    try:
        from nanobot.kernel.legacy import legacy_other_instance_roots

        return legacy_other_instance_roots(own)
    except Exception:  # noqa: BLE001 - the floor never fails open on discovery errors
        return []


def check_protected(
    path: Path,
    *,
    write: bool,
    workspace: Path | None,
    data_dir: DataDirs = None,
) -> None:
    """Raise ``ProtectedPathError`` when *path* is protected.

    With ``data_dir=None`` the default data dirs and the default config files
    (``get_config_path()``) are used. When an explicit *data_dir* is passed the
    config files are skipped: only the given data dirs and *workspace* are checked.
    """
    legacy = data_dir is None
    data_dirs = default_data_dirs() if legacy else data_dir
    reason = ProtectedFloor(
        data_dir=data_dirs,
        workspace=workspace,
        config_files=default_config_files() if legacy else (),
        other_instance_roots=default_other_instance_roots(workspace, data_dirs) if legacy else (),
    ).reason(path, write=write)
    if reason is not None:
        raise ProtectedPathError(reason)
