"""Load only the selected instance's owner-owned environment for CLI clients."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENTITY = {"HOME", "MOEKA_WORKSPACE", "MOEKA_STATE", "NANOBOT_HOME"}


def load_instance_environment(root: Path) -> None:
    """Parse KEY=VALUE data without executing shell code or changing instance identity."""
    for path in (root / ".env", root / "keys.env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if not _NAME.fullmatch(name) or name in _IDENTITY:
                continue
            parts = shlex.split(value, comments=True)
            if len(parts) > 1:
                raise ValueError(f"Invalid owner environment assignment in {path}: {name}")
            os.environ[name] = parts[0] if parts else ""
