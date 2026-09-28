"""One place for every legacy-shim deprecation message (Task 12).

The awork-compatibility surface (``nanobot.api.complete``, ``nanobot.core.MoekaCore``
/ ``MoekaKernel``, ``nanobot.core.vec.open_vec_store``) stays importable and
byte-for-byte behaviourally unchanged until awork migrates to ``moeka.Kernel``
(then it is deleted — see the plan's Task 15). Each shim's *only* change is one
:class:`DeprecationWarning` naming its exact replacement, raised through
:func:`warn_deprecated` here rather than as a scattered ad-hoc string, so every
shim's message stays in one greppable, testable place.

Kept dependency-free (only stdlib ``warnings``) so it can be imported from
``nanobot/api/complete.py``, which cannot import kernel modules at module load
time — see that module's docstring on the import-cycle constraint.
"""

from __future__ import annotations

import warnings

__all__ = ["warn_deprecated"]


def warn_deprecated(old: str, new: str, *, stacklevel: int = 3) -> None:
    """Emit one ``DeprecationWarning`` naming *old*'s exact replacement, *new*.

    ``stacklevel`` is relative to this frame; the default (``3``) fits the
    common shape — a public shim function/method calls ``warn_deprecated(...)``
    as its first statement, and that shim is what the host called directly —
    so the warning's ``filename``/``lineno`` land on the host's call site
    rather than on this helper or the shim itself (frame 1: this line, frame 2:
    the shim, frame 3: the shim's caller).
    """
    warnings.warn(
        f"{old} is deprecated; use {new} — see docs/python-sdk.md",
        DeprecationWarning,
        stacklevel=stacklevel,
    )
