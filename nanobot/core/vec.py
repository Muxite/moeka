"""Loop-less access to the semantic store — embeddings without an agent.

Hosts that only need local RAG (ingest text, search it) should not have to
construct a full :class:`MoekaCore` — that wires an ``AgentLoop`` with a
provider, sessions, and a message bus, and can fail in keyless environments.

::

    from nanobot.core.vec import open_vec_store

    store = open_vec_store("/data/knowledge/vec.db")
    if store.available:
        store.add_documents(text, source="notes.md")
        hits = store.search_documents_scored("deadline", k=5)
"""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.vec_store import VecStore

_DEFAULT_MODEL = "all-MiniLM-L6-v2"


def open_vec_store(db_path: str | Path, model: str | None = None) -> VecStore:
    """Open (or create) a standalone :class:`VecStore` at *db_path*.

    No agent loop, provider, or config file is involved. The store degrades
    gracefully: when ``moeka[vec]`` extras are missing, ``store.available`` is
    False and all operations are inert no-ops.

    Args:
        db_path: Path to the sqlite database file (parent dirs are created
            lazily on first write).
        model: sentence-transformers embedding model name; defaults to the
            same model the agent runtime uses.
    """
    return VecStore(Path(db_path), model_name=model or _DEFAULT_MODEL)
