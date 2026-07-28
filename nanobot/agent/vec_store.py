"""Back-compat shim — VecStore moved to :mod:`nanobot.core.vec_store`.

The semantic store is a standalone/core concern (usable loop-less via
:func:`nanobot.core.vec.open_vec_store`); the agent loop is just one
consumer. Import from ``nanobot.core.vec_store`` going forward.
"""

from nanobot.core.vec_store import VecStore

__all__ = ["VecStore"]
