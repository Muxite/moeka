"""Document memory: ``kernel.memory(scope)`` or ``kernel.memory(path=...)`` returns a
:class:`DocStore` (add text, then keyword / vector / hybrid ``search`` returning
:class:`Hit` s). All of a kernel's stores share one embedder. The methods are
synchronous SQLite; asyncio hosts call them via ``asyncio.to_thread``.
"""

from nanobot.kernel.memory import DocStore, Hit

__all__ = ["DocStore", "Hit"]
