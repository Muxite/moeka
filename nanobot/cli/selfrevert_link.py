"""Bridge to ``scripts/selfrevert.py`` (stdlib-only, so the boot guard works when this
package is broken). Returns ``None`` outside a git checkout, for example a pip install."""

from __future__ import annotations

import asyncio
import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from loguru import logger

HEALTHY_AFTER_S = 60.0


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    from nanobot.security.protected_paths import source_root

    root = source_root()
    return root if root is not None and (root / ".git").exists() else None


@lru_cache(maxsize=1)
def module() -> ModuleType | None:
    root = repo_root()
    script = root / "scripts" / "selfrevert.py" if root is not None else None
    if script is None or not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location("moeka_selfrevert", script)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def schedule_healthy_mark(state_dir: Path, after_s: float = HEALTHY_AFTER_S) -> None:
    """Tell the boot guard this process stayed up long enough to be called healthy."""
    mod = module()
    if mod is None:
        return

    def mark() -> None:
        try:
            mod.healthy(Path(state_dir))
        except Exception:
            logger.exception("could not record a healthy boot")

    asyncio.get_running_loop().call_later(after_s, mark)
