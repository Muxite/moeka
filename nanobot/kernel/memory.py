"""Document memory (Task 11): ``kernel.memory(scope)`` hands out :class:`DocStore` s.

A :class:`DocStore` is a small facade over one :class:`~nanobot.core.vec_store.VecStore`
file: host documents chunked, FTS5-indexed and (with ``moeka[vec]``) embedded, then
searched by keyword, vector or hybrid (reciprocal-rank fusion) retrieval.

Where the files live:
- ``kernel.memory(scope)`` -> ``<state_dir>/memory/<scope>.db``. A scope is a
  non-empty name without path separators, NUL or ``..``; it is percent-encoded
  into the file name (``agent:coach`` -> ``agent%3Acoach.db``), so distinct scopes
  never share a file. ``scope=None`` is ``"default"``.
- ``kernel.memory(path=...)`` opens a host-chosen file instead (awork's corpora).
- ``state_dir``, not ``work_dir``: this is kernel state the agent's file tools do
  not own (invariant I2).

One embedder per kernel: every store a kernel opens embeds through one lazily built
:class:`~nanobot.core.vec_store.SentenceTransformerEmbedder` (model
``config.agents.defaults.vec.embedding_model``), so N scopes load the model once.
A bare ``DocStore(path)`` without ``embedder=`` loads its own, as ``open_vec_store``
does.

Open files are bounded (an LRU, :data:`DEFAULT_MAX_OPEN` per kernel):
- ``kernel.memory`` returns the same handle for the same file while anyone holds it.
- Every call on a handle marks it most recently used. When more than the bound are
  open, the least recently used one is released (its WAL checkpointed and its
  connection closed). Its handle stays valid: the next call reopens the file
  transparently. Hence "handles never go stale"; the cost of an evicted scope is one
  reopen.
- A store an agent uses as its loop memory (``AgentSpec.memory``) is pinned while the
  agent is open and never evicted (the loop calls the VecStore directly).
- :meth:`DocStore.close` releases the file the same way (the handle reopens on use);
  on a pinned store it does nothing.
  After ``kernel.close()`` every handle it gave out raises ``RuntimeError``.

Concurrency: every method is synchronous SQLite work under a per-handle lock, safe
from any thread. There are deliberately no ``async`` variants: an asyncio host calls
them through ``await asyncio.to_thread(store.search, ...)`` (a search that embeds its
query can take tens of milliseconds, too long for an event loop).

Agents: ``AgentSpec.doc_scopes`` gives the agent a read-only ``search_documents``
action over those scopes (:func:`search_documents_tool`), registered like any other
action, so the tool scope, policy and offline rules apply.
"""

from __future__ import annotations

import json
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

from nanobot.core.vec_store import SentenceTransformerEmbedder, VecStore

if TYPE_CHECKING:
    from nanobot.core.vec_store import Embedder

DEFAULT_SCOPE = "default"
DEFAULT_MAX_OPEN = 32
MEMORY_DIRNAME = "memory"
SEARCH_TOOL_NAME = "search_documents"
_SEARCH_MODES = frozenset({"vec", "keyword", "hybrid"})
_MAX_SCOPE_LEN = 128
_MAX_TOOL_K = 20

_T = TypeVar("_T")


def validate_scope(scope: Any) -> str:
    """*scope* if it is a usable scope name, else ``ValueError``."""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError(f"memory scope must be a non-empty string, got {scope!r}")
    if len(scope) > _MAX_SCOPE_LEN:
        raise ValueError(f"memory scope is longer than {_MAX_SCOPE_LEN} characters")
    if any(c in scope for c in ("/", "\\", "\x00")) or ".." in scope:
        raise ValueError(
            f"memory scope {scope!r} must not contain path separators, NUL or '..'"
        )
    return scope


def scope_filename(scope: str) -> str:
    """The file name a scope is stored in (percent-encoded, so injective)."""
    return quote(validate_scope(scope), safe="-_.") + ".db"


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk. ``score``: lower is better (see ``DocStore.search``)."""

    source: str | None
    text: str
    score: float
    tags: tuple[str, ...] = ()
    collection: str = "default"


def _since_iso(since: datetime | str | None) -> str | None:
    if since is None or isinstance(since, str):
        return since
    if isinstance(since, datetime):
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware (chunks are stamped in UTC)")
        return since.astimezone(timezone.utc).isoformat()
    raise TypeError(f"since must be a datetime, an ISO string or None, got {since!r}")


class DocStore:
    """Documents in one SQLite file: ``add`` text, then ``search`` it.

    Build through ``kernel.memory(...)`` (shared embedder, bounded open files); a bare
    ``DocStore(path)`` works too. See the module docstring for lifetime and threads.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        embedder: Embedder | None = None,
        log_retrievals: bool = False,
        scope: str | None = None,
        _registry: _MemoryRegistry | None = None,
    ) -> None:
        self._path = Path(path)
        self._scope = scope
        self._embedder = embedder
        self._log_retrievals = log_retrievals
        self._registry = _registry
        self._lock = threading.RLock()
        self._vec: VecStore | None = None
        self._pins = 0
        self._shut = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def scope(self) -> str | None:
        """The scope name (``None`` for a store opened by path)."""
        return self._scope

    # -- plumbing -------------------------------------------------------------

    def _store(self) -> VecStore:
        """The VecStore, built on first use (call with the lock held)."""
        if self._shut:
            raise RuntimeError("kernel is closed: its document stores cannot be used")
        if self._vec is None:
            kwargs: dict[str, Any] = {"log_retrievals": self._log_retrievals}
            if self._embedder is not None:
                kwargs["embedder"] = self._embedder
            self._vec = VecStore(self._path, **kwargs)
        return self._vec

    def _run(self, fn: Callable[[VecStore], _T]) -> _T:
        with self._lock:
            result = fn(self._store())
        if self._registry is not None:
            self._registry._touch(self)
        return result

    def _release(self) -> None:
        """Checkpoint and close the file; the next call reopens it."""
        with self._lock:
            if self._vec is not None:
                self._vec.close()

    def _shutdown(self) -> None:
        with self._lock:
            self._release()
            self._shut = True

    def _pin(self) -> VecStore:
        """Pin against LRU eviction and return the (open) VecStore."""
        with self._lock:
            self._pins += 1
            store = self._store()
        if self._registry is not None:
            self._registry._touch(self)
        return store

    def _pin_store(self) -> VecStore:
        """The VecStore of a pinned handle (the one an agent loop holds)."""
        with self._lock:
            if not self._pins:
                raise RuntimeError("only a pinned DocStore hands out its VecStore")
            return self._store()

    def _unpin(self) -> None:
        with self._lock:
            self._pins = max(0, self._pins - 1)

    @property
    def _pinned(self) -> bool:
        return self._pins > 0

    # -- API ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Vector (semantic) retrieval works: sqlite-vec and an embedder are present."""
        return self._run(lambda v: v.available)

    @property
    def keyword_available(self) -> bool:
        """FTS5 keyword retrieval works (stock sqlite3; no embeddings needed)."""
        return self._run(lambda v: v.keyword_available)

    def add(
        self,
        text: str,
        *,
        source: str | None = None,
        tags: Iterable[str] = (),
        collection: str = "default",
    ) -> int:
        """Chunk, index and store *text*; return the number of chunks stored.

        Returns 0 for blank text or when neither retrieval backend is usable.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be a str, got {type(text).__name__}")
        tag_list = _tags(tags)
        return self._run(lambda v: v.add_documents(
            text, source, collection=collection, tags=tag_list or None,
        ))

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        mode: str = "hybrid",
        tags: Iterable[str] | None = None,
        since: datetime | str | None = None,
        collection: str | None = "default",
    ) -> list[Hit]:
        """The top *k* chunks for *query*, best first.

        ``mode``: ``"hybrid"`` (reciprocal-rank fusion of vector and keyword results;
        falls back to whichever backend is available), ``"vec"`` or ``"keyword"``.
        ``score`` is lower-is-better in every mode (cosine distance, BM25 rank, or the
        negated fusion score). ``tags``: every listed tag must be present. ``since``:
        only chunks added at or after it. ``collection=None`` searches all collections.
        """
        if mode not in _SEARCH_MODES:
            raise ValueError(f"unknown search mode {mode!r}; use one of {sorted(_SEARCH_MODES)}")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be a positive int, got {k!r}")
        tag_list = _tags(tags) if tags is not None else None
        since_iso = _since_iso(since)
        rows = self._run(lambda v: v.search_documents_detailed(
            query, k, collection=collection, mode=mode, tags=tag_list or None,
            since=since_iso,
        ))
        return [
            Hit(source=r["source"], text=r["text"], score=float(r["score"]),
                tags=tuple(r["tags"]), collection=r["collection"])
            for r in rows
        ]

    def count(self, *, collection: str | None = None, source: str | None = None) -> int:
        """Stored chunks (``collection=None``: all collections; ``source`` filters)."""
        return self._run(lambda v: v.count_documents(collection=collection, source=source))

    def sources(self, *, collection: str | None = None) -> dict[str, int]:
        """Chunk count per source (chunks added without a source are not listed)."""
        return self._run(lambda v: v.document_sources(collection=collection))

    def clear(self, *, collection: str | None = None, source: str | None = None) -> None:
        """Delete chunks: everything by default, else one collection and/or source."""
        self._run(lambda v: v.clear_documents(collection=collection, source=source))

    def get_meta(self, key: str) -> str | None:
        """A host key/value (stored in the file's ``meta`` table), ``None`` if unset."""
        return self._run(lambda v: v.get_meta(key))

    def set_meta(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"meta values are strings, got {type(value).__name__}")
        self._run(lambda v: v.set_meta(key, value))

    def close(self) -> None:
        """Checkpoint and release the file. The handle stays usable (it reopens).

        A no-op while an agent is using the store as its loop memory (pinned): the
        agent's loop holds the connection, and the kernel closes it with the agent.
        """
        with self._lock:
            if self._pins:
                return
            self._release()

    def __enter__(self) -> DocStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        what = f"scope={self._scope!r}" if self._scope is not None else f"path={str(self._path)!r}"
        return f"DocStore({what})"


def _tags(tags: Iterable[str]) -> list[str]:
    if isinstance(tags, str):
        raise TypeError("tags must be an iterable of strings, not a str")
    out = list(tags)
    for tag in out:
        if not isinstance(tag, str) or not tag:
            raise ValueError(f"tags must be non-empty strings, got {tag!r}")
    return out


class _MemoryRegistry:
    """A kernel's document stores: one handle per file, one embedder, an LRU of open files."""

    def __init__(
        self,
        state_dir: Path,
        *,
        embedding_model: str,
        log_retrievals: bool = False,
        capacity: int = DEFAULT_MAX_OPEN,
    ) -> None:
        self._root = Path(state_dir) / MEMORY_DIRNAME
        self._embedding_model = embedding_model
        self._log_retrievals = log_retrievals
        self.capacity = capacity
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None
        self._handles: weakref.WeakValueDictionary[Path, DocStore] = (
            weakref.WeakValueDictionary()
        )
        # Open handles, least recently used first (strong refs).
        self._open: OrderedDict[Path, DocStore] = OrderedDict()
        self._closed = False

    @property
    def embedder(self) -> Embedder:
        """The kernel's one embedder (built on first use; the model loads on first embed)."""
        with self._lock:
            return self._embedder_locked()

    def _embedder_locked(self) -> Embedder:
        if self._embedder is None:
            self._embedder = SentenceTransformerEmbedder(self._embedding_model)
        return self._embedder

    def get(self, scope: str | None, path: str | Path | None) -> DocStore:
        if scope is not None and path is not None:
            raise ValueError("pass a memory scope or a path, not both")
        if path is not None:
            file = Path(path).resolve()
            name: str | None = None
        else:
            name = DEFAULT_SCOPE if scope is None else validate_scope(scope)
            file = self._root / scope_filename(name)
        with self._lock:
            if self._closed:
                raise RuntimeError("kernel is closed")
            store = self._handles.get(file)
            if store is None:
                store = DocStore(
                    file, embedder=self._embedder_locked(),
                    log_retrievals=self._log_retrievals, scope=name, _registry=self,
                )
                self._handles[file] = store
            return store

    def _touch(self, store: DocStore) -> None:
        """Mark *store* most recently used; release the least recent beyond capacity."""
        victims: list[DocStore] = []
        with self._lock:
            if self._closed:
                return
            key = store.path
            if key in self._open:
                self._open.move_to_end(key)
            else:
                self._open[key] = store
            excess = len(self._open) - self.capacity
            if excess > 0:
                for victim_key, victim in list(self._open.items()):
                    if excess <= 0:
                        break
                    if victim is store or victim._pinned:
                        continue
                    del self._open[victim_key]
                    victims.append(victim)
                    excess -= 1
        # Outside the registry lock: a victim busy in another thread finishes its
        # call first (its own lock), then closes.
        for victim in victims:
            victim._release()

    def open_paths(self) -> list[Path]:
        """Files currently counted as open, least recently used first (for tests)."""
        with self._lock:
            return list(self._open)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stores = {id(s): s for s in [*self._handles.values(), *self._open.values()]}
            self._open.clear()
        for store in stores.values():
            store._shutdown()


# -- the agent action ----------------------------------------------------------------


def search_documents_tool(stores: Callable[[str], DocStore], scopes: Sequence[str]) -> Any:
    """The read-only ``search_documents(query, scope=None, k=5)`` action over *scopes*.

    ``stores(scope)`` returns the scope's :class:`DocStore` (``kernel.memory``). The
    search is hybrid; with no ``scope`` every scope is searched and the results are
    interleaved by rank. An unknown scope is a tool error listing the allowed ones.
    """
    import asyncio

    from nanobot.agent.tools.base import ToolResult
    from nanobot.core.function_tool import FunctionTool

    allowed = tuple(dict.fromkeys(validate_scope(s) for s in scopes))
    if not allowed:
        raise ValueError("search_documents needs at least one scope")

    def _search(query: str, scope: str | None, k: int) -> list[dict[str, Any]]:
        targets = [scope] if scope is not None else list(allowed)
        per_scope = [
            [(name, hit) for hit in stores(name).search(query, k=k, mode="hybrid")]
            for name in targets
        ]
        merged: list[tuple[str, Hit]] = []
        for rank in range(k):
            for hits in per_scope:
                if rank < len(hits):
                    merged.append(hits[rank])
        return [
            {"scope": name, "source": hit.source, "text": hit.text}
            for name, hit in merged[:k]
        ]

    async def search_documents(query: str, scope: str | None = None, k: int = 5) -> str:
        if scope is not None and scope not in allowed:
            return ToolResult.error(
                f"Error: unknown document scope {scope!r}; allowed scopes: "
                + ", ".join(allowed)
            )
        if not isinstance(query, str) or not query.strip():
            return ToolResult.error("Error: query must be a non-empty string")
        k = max(1, min(int(k), _MAX_TOOL_K))
        results = await asyncio.to_thread(_search, query, scope, k)
        if not results:
            return "No matching documents."
        return json.dumps(results, ensure_ascii=False)

    description = (
        "Search the host's document stores (hybrid keyword + semantic retrieval) and "
        "return the best matching passages with their source. Scopes: "
        + ", ".join(allowed)
        + ". Omit scope to search all of them."
    )
    return FunctionTool(
        search_documents, name=SEARCH_TOOL_NAME, description=description, read_only=True,
    )


__all__ = [
    "DEFAULT_MAX_OPEN",
    "DEFAULT_SCOPE",
    "SEARCH_TOOL_NAME",
    "DocStore",
    "Hit",
    "scope_filename",
    "search_documents_tool",
    "validate_scope",
]
