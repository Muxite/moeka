"""Real-``$HOME`` constants shared between the root ``conftest.py`` and
``tests/conftest.py`` test-isolation guards.

Not a conftest itself (no ``test_*`` name, not picked up by pytest's conftest
loader) -- a plain module, imported by both. It exists so the two guards
agree on exactly one definition of "the real home," computed exactly once,
before pytest's root ``conftest.py`` redirects ``$HOME`` for the test
session. Python caches modules on first import: as long as the *root*
``conftest.py`` is the first thing to import this module (guaranteed --
pytest always imports the rootdir's ``conftest.py`` before descending into
``tests/`` for collection, and this module's top-level code runs at that
first import, before ``pytest_configure`` redirects ``$HOME``), every later
importer -- including ``tests/conftest.py``, imported during collection,
after the redirect -- gets back the *same*, already-computed real values
rather than recomputing them against the now-fake ``$HOME``.

That "recompute after redirect" bug is exactly what this module exists to
prevent: ``tests/conftest.py``'s ``_guard_live_workspace`` fixture used to
compute ``Path.home() / ".nanobot"`` at *fixture* time -- after the redirect
-- so it resolved to the fake home and could never fire for the real one.
See the review-fixes section of ``.agent/upstream-sync-notes.md``.
"""

from __future__ import annotations

import os
from pathlib import Path

REAL_HOME = Path(os.environ.get("HOME") or os.path.expanduser("~")).resolve()

# Guard specific nanobot/moeka state locations under the real $HOME, not the
# whole home directory -- the project checkout, .cache, .pytest_cache, etc.
# all legitimately live under $HOME too and must stay writable.
REAL_NANOBOT_HOME = REAL_HOME / ".nanobot"
REAL_NANOBOT_SESSIONS_HOME = REAL_HOME / f"{REAL_NANOBOT_HOME.name}-sessions"
GUARDED_REAL_PATHS = (REAL_NANOBOT_HOME, REAL_NANOBOT_SESSIONS_HOME)


def path_is_guarded(path: object) -> Path | None:
    """Return the guarded real-home path *path* resolves under, or ``None``.

    Shared by every guard (mkdir, rename/move, sqlite3.connect, ...) so they
    all use identical resolution semantics.
    """
    try:
        resolved = Path(os.fspath(path)).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return None
    for guarded in GUARDED_REAL_PATHS:
        if resolved == guarded or resolved.is_relative_to(guarded):
            return guarded
    return None
