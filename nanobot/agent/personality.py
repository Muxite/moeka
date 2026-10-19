"""The sole agent-writable personality surface, staged until the next turn."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock

from nanobot.session.manager import JsonlSessionStore

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment


def settings(env: CoreEnvironment | None) -> dict[str, Any]:
    return env.config.section("personality") if env is not None else {}


def _root(workspace: Path) -> Path:
    root = workspace / ".personality"
    if root.is_symlink() or (workspace / "SOUL.md").is_symlink():
        raise PermissionError("Personality paths must not be symlinks")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def validate(text: str, limit: int = 150) -> str:
    text = text.strip()
    if not text or len(text.split()) > min(limit, 150):
        raise ValueError(f"Personality must contain 1 to {min(limit, 150)} words")
    return text + "\n"


def stage(workspace: Path, text: str, env: CoreEnvironment | None) -> str:
    cfg = settings(env)
    if not cfg.get("allow_self_edit", False):
        raise PermissionError("Personality editing is disabled for this instance")
    text = validate(text, int(cfg.get("max_words", 150)))
    root = _root(workspace)
    with FileLock(str(root / "lock")):
        JsonlSessionStore._write_text_atomic(root / "pending", text)  # pyright: ignore[reportPrivateUsage]
    return "Personality revision staged; it applies at the next turn. Fixed rules are unchanged."


def activate(workspace: Path, env: CoreEnvironment | None) -> None:
    if not settings(env).get("allow_self_edit", False):
        return
    root = _root(workspace)
    with FileLock(str(root / "lock")):
        pending = root / "pending"
        if not pending.exists():
            return
        text = validate(pending.read_text(), int(settings(env).get("max_words", 150)))
        soul = workspace / "SOUL.md"
        for content in (soul.read_text() if soul.exists() else "", text):
            if content.strip():
                digest = hashlib.sha256(content.encode()).hexdigest()
                path = root / f"{digest}.md"
                if not path.exists():
                    JsonlSessionStore._write_text_atomic(path, content)  # pyright: ignore[reportPrivateUsage]
        JsonlSessionStore._write_text_atomic(soul, text)  # pyright: ignore[reportPrivateUsage]
        pending.unlink()


def read_personality(workspace: Path, text: str, env: CoreEnvironment | None) -> str:
    # Loading does not activate: repeated prompt renders in a turn see one revision.
    return text.replace("{{name}}", str(settings(env).get("name", "Moeka")))


def versions(workspace: Path) -> list[str]:
    root = workspace / ".personality"
    if root.is_symlink():
        raise PermissionError("Personality history must not be a symlink")
    return sorted(p.stem for p in root.glob("*.md"))


def restore(workspace: Path, version: str, env: CoreEnvironment | None) -> str:
    if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        raise ValueError("Invalid personality version")
    path = _root(workspace) / f"{version}.md"
    text = path.read_text()
    if hashlib.sha256(text.encode()).hexdigest() != version:
        raise ValueError("Personality history hash mismatch")
    return stage(workspace, text, env)
