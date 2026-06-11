"""Loop-less access to moeka's semantic store.

:func:`open_vec_store` returns a bare :class:`~nanobot.core.vec_store.VecStore`
— no AgentLoop, provider, or config — so a host can use ``moeka[vec]`` as a
standalone local embeddings library. No API keys are required; in a keyless or
extras-less environment the store degrades to inert (``.available is False``)
rather than raising.

    from nanobot.core.vec import open_vec_store

    store = open_vec_store("/path/to/vec.db")
    store.add_documents(text, source="notes.md", collection="knowledge")
    for source, chunk, score in store.search_documents_scored("query", k=5):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nanobot.core.vec_store import VecStore

__all__ = ["RetrievedChunk", "VecStore", "open_vec_store"]

_DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class RetrievedChunk:
    """One semantically retrieved document chunk with attribution and score."""

    text: str
    source: str | None
    score: float  # cosine distance; lower is closer


def open_vec_store(
    db_path: str | Path,
    *,
    model: str | None = None,
    log_retrievals: bool = False,
) -> VecStore:
    """Open (or create) a :class:`VecStore` at *db_path* — embeddings only.

    ``model=None`` uses the default embedding model. No AgentLoop, provider,
    or API key is involved; safe in keyless environments. When ``moeka[vec]``
    extras are missing the returned store degrades to FTS5 keyword search
    (``.available is False`` but ``.keyword_available`` may stay True).
    ``log_retrievals=True`` records every search to the ``retrieval_log``
    table for observability.
    """
    return VecStore(
        Path(db_path),
        model_name=model or _DEFAULT_EMBEDDING_MODEL,
        log_retrievals=log_retrievals,
    )
