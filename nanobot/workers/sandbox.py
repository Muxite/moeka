"""bwrap wrapper for worker processes (spec 015).

Not the exec-tool sandbox: a worker must outlive a gateway restart (no ``--die-with-parent``),
needs its own login directory and binary, and needs working DNS. What it never gets: the
owner's other projects, moeka's own state, ssh keys, or moeka's source tree.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from nanobot.agent.tools.sandbox import granted_root_masks

_RO_SYSTEM = (
    "/usr", "/bin", "/lib", "/lib64", "/etc", "/run/systemd/resolve", "/opt",
)


def worker_bwrap_argv(
    argv: list[str], *, workspace: Path, home: Path, state_home: Path,
    ro_binds: Iterable[Path] = (), rw_binds: Iterable[Path] = (),
    env: dict[str, str] | None = None,
) -> list[str]:
    """*argv* wrapped in a bubblewrap sandbox.

    - ``workspace`` is bound read-write and is the working directory.
    - ``state_home`` is the real home: only ``home`` (the backend's login/session dir) and
      the paths in *ro_binds*/*rw_binds* are mounted under a fresh tmpfs.
    - Moeka's source tree is re-bound read-only and dotenv files under the workspace are
      masked with an empty file (``granted_root_masks``).
    """
    ws = workspace.resolve()
    args = [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--cap-drop", "ALL", "--new-session",
    ]
    for key, value in (env or {}).items():
        args += ["--setenv", key, value]
    for path in _RO_SYSTEM:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", str(state_home)]
    for path in ro_binds:
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for path in (home, *rw_binds):
        if path.exists():
            args += ["--bind", str(path), str(path)]
    args += ["--bind", str(ws), str(ws)]
    args += granted_root_masks([str(ws)])
    args += ["--chdir", str(ws), "--", *argv]
    return args
