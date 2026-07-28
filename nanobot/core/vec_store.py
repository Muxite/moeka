"""Semantic vector store backed by sqlite-vec + sentence-transformers.

Provides four stores:
  - memory_chunks   — MEMORY.md split by section headers
  - history_entries — history.jsonl entries
  - skills          — skill definitions (indexed at startup)
  - documents       — host-supplied text (append-only), optionally split
                      into named *collections* so a host can keep separate
                      corpora in one vec.db

Documents carry metadata (``source``, ``tags``, ``created_at``) and support
three retrieval modes: ``vec`` (semantic KNN), ``keyword`` (FTS5/BM25, works
without sentence-transformers), and ``hybrid`` (reciprocal-rank fusion of
both). Schema changes are versioned via ``PRAGMA user_version``; a ``meta``
table records the embedding model + dimension so a model change triggers an
automatic re-embed of all stores from their text rows.

All public methods degrade gracefully: if sqlite-vec or
sentence-transformers are unavailable the caller gets None/[] back (keyword
search keeps working with stock sqlite3 + FTS5). Import this module safely;
it never raises at import time.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import numpy as np

_SECTION_RE = re.compile(r"^#{1,3} .+", re.MULTILINE)
_MAX_CHUNK_CHARS = 500
_EMBEDDING_DIM = 384
_SCHEMA_VERSION = 2
_RRF_K = 60  # standard reciprocal-rank-fusion constant
_VEC_STORES = ("memory_chunks", "history_entries", "skills", "documents")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_markdown(text: str) -> list[str]:
    """Split markdown on section headers; each chunk = heading + its content."""
    if not text.strip():
        return []
    boundaries = [m.start() for m in _SECTION_RE.finditer(text)]
    if not boundaries:
        return [text[:_MAX_CHUNK_CHARS]] if text.strip() else []
    chunks: list[str] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk[:_MAX_CHUNK_CHARS])
    return chunks


def _chunk_document(text: str) -> list[str]:
    """Split arbitrary text into <=_MAX_CHUNK_CHARS windows without truncating.

    Prefers markdown-section boundaries when present, then packs paragraphs into
    size-bounded chunks, hard-splitting any single paragraph that overflows. Unlike
    :func:`_chunk_markdown`, no content is dropped — suitable for host documents.
    """
    if not text.strip():
        return []
    units = _chunk_markdown(text) if _SECTION_RE.search(text) else _split_paragraphs(text)
    chunks: list[str] = []
    for unit in units:
        unit = unit.strip()
        while len(unit) > _MAX_CHUNK_CHARS:
            cut = unit.rfind(" ", 0, _MAX_CHUNK_CHARS)
            if cut <= 0:
                cut = _MAX_CHUNK_CHARS
            chunks.append(unit[:cut].strip())
            unit = unit[cut:].strip()
        if unit:
            chunks.append(unit)
    return chunks


def _split_paragraphs(text: str) -> list[str]:
    """Pack blank-line-separated paragraphs into <=_MAX_CHUNK_CHARS groups."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    groups: list[str] = []
    buf = ""
    for para in paras:
        if buf and len(buf) + len(para) + 2 > _MAX_CHUNK_CHARS:
            groups.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        groups.append(buf)
    return groups


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms."""
    terms = [t for t in re.findall(r"\w+", query) if t]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


class VecStore:
    """sqlite-vec backed semantic store for memory, history, skills, and documents."""

    def __init__(
        self,
        db_path: Path,
        model_name: str = "all-MiniLM-L6-v2",
        *,
        log_retrievals: bool = False,
    ) -> None:
        self._db_path = db_path
        self._model_name = model_name
        self._model = None  # lazy-loaded
        self._conn: sqlite3.Connection | None = None
        self._vec_loaded = False  # sqlite-vec extension active on the connection
        self._fts_available = False
        self._embed_dim_checked = False
        self._log_retrievals = log_retrievals
        self._available = self._try_init()

    # ------------------------------------------------------------------
    # Public availability checks
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Semantic (vector) retrieval is usable."""
        return self._available

    @property
    def keyword_available(self) -> bool:
        """FTS5 keyword retrieval over documents is usable (no embeddings needed)."""
        return self._fts_available

    # ------------------------------------------------------------------
    # Memory chunks
    # ------------------------------------------------------------------

    def upsert_memory_chunks(self, text: str) -> None:
        """Re-index all MEMORY.md content (full replace)."""
        if not self._available:
            return
        chunks = _chunk_markdown(text)
        try:
            conn = self._connection()
            conn.execute("DELETE FROM memory_chunks_data")
            conn.execute("DELETE FROM memory_chunks_vec")
            if chunks:
                embeddings = self._embed(chunks)
                for chunk, emb in zip(chunks, embeddings):
                    cur = conn.execute(
                        "INSERT INTO memory_chunks_data(text) VALUES (?)", (chunk,)
                    )
                    conn.execute(
                        "INSERT INTO memory_chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (cur.lastrowid, emb.tobytes()),
                    )
            conn.commit()
            logger.debug("VecStore: indexed {} memory chunk(s)", len(chunks))
        except Exception:
            logger.exception("VecStore: upsert_memory_chunks failed")

    def search_memory(self, query: str, k: int = 5, *, caller: str | None = None) -> list[str]:
        """Return the top-k memory chunks semantically closest to *query*."""
        results = self._search_simple_store("memory_chunks", query, k)
        self._log_retrieval("memory_chunks", caller, query, k, results)
        return results

    # ------------------------------------------------------------------
    # History entries
    # ------------------------------------------------------------------

    def upsert_history_entry(self, cursor: int, text: str) -> None:
        """Index a single history.jsonl entry by its cursor value."""
        if not self._available or not text.strip():
            return
        try:
            conn = self._connection()
            existing = conn.execute(
                "SELECT id FROM history_entries_data WHERE id = ?", (cursor,)
            ).fetchone()
            if existing:
                return  # already indexed
            emb = self._embed([text])[0]
            conn.execute(
                "INSERT INTO history_entries_data(id, text) VALUES (?, ?)", (cursor, text)
            )
            conn.execute(
                "INSERT INTO history_entries_vec(rowid, embedding) VALUES (?, ?)",
                (cursor, emb.tobytes()),
            )
            conn.commit()
        except Exception:
            logger.exception("VecStore: upsert_history_entry failed")

    def search_history(self, query: str, k: int = 5, *, caller: str | None = None) -> list[str]:
        """Return the top-k history entries semantically closest to *query*."""
        results = self._search_simple_store("history_entries", query, k)
        self._log_retrieval("history_entries", caller, query, k, results)
        return results

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    def upsert_skills(self, skills: list[tuple[str, str]]) -> None:
        """Re-index all skills (full replace). *skills* is a list of (name, text)."""
        if not self._available or not skills:
            return
        try:
            conn = self._connection()
            conn.execute("DELETE FROM skills_data")
            conn.execute("DELETE FROM skills_vec")
            texts = [f"{name}: {text}" for name, text in skills]
            embeddings = self._embed(texts)
            for text, emb in zip(texts, embeddings):
                cur = conn.execute(
                    "INSERT INTO skills_data(text) VALUES (?)", (text,)
                )
                conn.execute(
                    "INSERT INTO skills_vec(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, emb.tobytes()),
                )
            conn.commit()
            logger.debug("VecStore: indexed {} skill(s)", len(skills))
        except Exception:
            logger.exception("VecStore: upsert_skills failed")

    def search_skills(self, query: str, k: int = 5, *, caller: str | None = None) -> list[str]:
        """Return the top-k skill texts semantically closest to *query*."""
        results = self._search_simple_store("skills", query, k)
        self._log_retrieval("skills", caller, query, k, results)
        return results

    def _search_simple_store(self, store: str, query: str, k: int) -> list[str]:
        """Shared KNN search over the single-text stores (memory/history/skills)."""
        if not self._available or not query.strip():
            return []
        try:
            conn = self._connection()
            count = conn.execute(f"SELECT count(*) FROM {store}_data").fetchone()[0]
            if count == 0:
                return []
            emb = self._embed([query])[0]
            limit = min(k, count)
            rows = conn.execute(
                f"""
                SELECT d.text
                FROM {store}_vec v
                JOIN {store}_data d ON d.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (emb.tobytes(), limit),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            logger.exception("VecStore: search over {} failed", store)
            return []

    # ------------------------------------------------------------------
    # Host documents (incremental, append-only)
    # ------------------------------------------------------------------

    def add_documents(
        self,
        text: str,
        source: str | None = None,
        *,
        collection: str = "default",
        tags: list[str] | None = None,
    ) -> int:
        """Chunk and index host-supplied *text* incrementally. Returns chunk count.

        ``tags`` are stored as a JSON list and filterable at search time.
        When embeddings are unavailable the chunks are still stored (and
        FTS5-indexed) so keyword retrieval keeps working.
        """
        if not (self._available or self._fts_available) or not text.strip():
            return 0
        chunks = _chunk_document(text)
        if not chunks:
            return 0
        try:
            conn = self._connection()
            tags_json = json.dumps(tags) if tags else None
            created = _now_iso()
            embeddings = self._embed(chunks) if self._available else [None] * len(chunks)
            for chunk, emb in zip(chunks, embeddings):
                cur = conn.execute(
                    "INSERT INTO documents_data(source, text, collection, tags, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (source, chunk, collection, tags_json, created),
                )
                if emb is not None:
                    conn.execute(
                        "INSERT INTO documents_vec(rowid, embedding) VALUES (?, ?)",
                        (cur.lastrowid, emb.tobytes()),
                    )
            conn.commit()
            logger.debug("VecStore: indexed {} document chunk(s)", len(chunks))
            return len(chunks)
        except Exception:
            logger.exception("VecStore: add_documents failed")
            return 0

    def search_documents(
        self,
        query: str,
        k: int = 5,
        *,
        collection: str | None = "default",
        mode: str = "vec",
        tags: list[str] | None = None,
        since: str | None = None,
        caller: str | None = None,
    ) -> list[str]:
        """Return the top-k host-document chunks for *query* (best first)."""
        return [
            text for _source, text, _score in self.search_documents_scored(
                query, k=k, collection=collection, mode=mode,
                tags=tags, since=since, caller=caller,
            )
        ]

    def search_documents_scored(
        self,
        query: str,
        k: int = 5,
        *,
        collection: str | None = "default",
        mode: str = "vec",
        tags: list[str] | None = None,
        since: str | None = None,
        caller: str | None = None,
    ) -> list[tuple[str | None, str, float]]:
        """Top-k host-document chunks as ``(source, text, score)``, best first.

        Modes:
          - ``"vec"``     — semantic KNN; score is cosine distance (lower = closer).
          - ``"keyword"`` — FTS5/BM25; score is the BM25 rank value (lower = better).
            Works without sentence-transformers.
          - ``"hybrid"``  — reciprocal-rank fusion of both; score is the negated
            RRF value so lower = better, consistent with the other modes. Falls
            back to whichever side is available.

        Filters: ``collection`` (None = all), ``tags`` (all listed tags must be
        present), ``since`` (ISO timestamp; only chunks indexed at/after it).
        """
        if mode not in ("vec", "keyword", "hybrid"):
            raise ValueError(f"unknown search mode {mode!r}")
        if not query.strip():
            return []
        try:
            results = self._search_documents_inner(
                query, k, collection=collection, mode=mode, tags=tags, since=since,
            )
        except Exception:
            logger.exception("VecStore: search_documents_scored failed")
            return []
        self._log_retrieval(
            "documents", caller, query, k,
            [text for _s, text, _d in results],
            mode=mode, collection=collection,
        )
        return results

    def _search_documents_inner(
        self,
        query: str,
        k: int,
        *,
        collection: str | None,
        mode: str,
        tags: list[str] | None,
        since: str | None,
    ) -> list[tuple[str | None, str, float]]:
        vec_ok = self._available
        fts_ok = self._fts_available
        if mode == "vec" and not vec_ok:
            return []
        if mode == "keyword" and not fts_ok:
            return []
        if mode == "hybrid":
            if vec_ok and fts_ok:
                vec_rows = self._vec_documents(query, k, collection, tags, since)
                kw_rows = self._keyword_documents(query, k, collection, tags, since)
                return self._rrf_fuse(vec_rows, kw_rows, k)
            mode = "vec" if vec_ok else "keyword"
            if mode == "vec" and not vec_ok:
                return []
            if mode == "keyword" and not fts_ok:
                return []
        if mode == "keyword":
            return [(s, t, score) for _id, s, t, score in
                    self._keyword_documents(query, k, collection, tags, since)]
        return [(s, t, score) for _id, s, t, score in
                self._vec_documents(query, k, collection, tags, since)]

    def _doc_filter_sql(
        self, collection: str | None, tags: list[str] | None, since: str | None,
    ) -> tuple[str, list]:
        """WHERE-clause fragments (ANDed) + params for document metadata filters."""
        sql = ""
        params: list = []
        if collection is not None:
            sql += " AND d.collection = ?"
            params.append(collection)
        for tag in tags or ():
            sql += (
                " AND d.tags IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM json_each(d.tags) WHERE json_each.value = ?)"
            )
            params.append(tag)
        if since is not None:
            sql += " AND d.created_at >= ?"
            params.append(since)
        return sql, params

    def _vec_documents(
        self,
        query: str,
        k: int,
        collection: str | None,
        tags: list[str] | None,
        since: str | None,
    ) -> list[tuple[int, str | None, str, float]]:
        """Semantic KNN rows as ``(id, source, text, distance)``, closest first."""
        conn = self._connection()
        total = conn.execute("SELECT count(*) FROM documents_data").fetchone()[0]
        if total == 0:
            return []
        filter_sql, filter_params = self._doc_filter_sql(collection, tags, since)
        in_scope = total
        if filter_sql:
            in_scope = conn.execute(
                f"SELECT count(*) FROM documents_data d WHERE 1=1{filter_sql}",
                filter_params,
            ).fetchone()[0]
            if in_scope == 0:
                return []
        emb = self._embed([query])[0]
        want = min(k, in_scope)
        # Metadata predicates filter *after* the KNN returns its k nearest
        # rows, so with mixed data a plain k would under-return. Over-fetch,
        # escalating to a full scan if needed (vec0 brute-forces either way;
        # only row materialization grows).
        knn_k = want if not filter_sql or in_scope == total else min(
            total, max(want * 4, 32)
        )
        while True:
            sql = f"""
                SELECT d.id, d.source, d.text, v.distance
                FROM documents_vec v
                JOIN documents_data d ON d.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?{filter_sql}
                ORDER BY distance
            """
            rows = conn.execute(sql, [emb.tobytes(), knn_k, *filter_params]).fetchall()
            out = [(r[0], r[1], r[2], float(r[3])) for r in rows]
            if len(out) >= want or knn_k >= total:
                return out[:want]
            knn_k = total

    def _keyword_documents(
        self,
        query: str,
        k: int,
        collection: str | None,
        tags: list[str] | None,
        since: str | None,
    ) -> list[tuple[int, str | None, str, float]]:
        """FTS5/BM25 rows as ``(id, source, text, bm25)``, best first."""
        match = _fts_query(query)
        if not match:
            return []
        conn = self._connection()
        filter_sql, filter_params = self._doc_filter_sql(collection, tags, since)
        sql = f"""
            SELECT d.id, d.source, d.text, bm25(documents_fts) AS rank
            FROM documents_fts f
            JOIN documents_data d ON d.id = f.rowid
            WHERE documents_fts MATCH ?{filter_sql}
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, [match, *filter_params, k]).fetchall()
        return [(r[0], r[1], r[2], float(r[3])) for r in rows]

    @staticmethod
    def _rrf_fuse(
        vec_rows: list[tuple[int, str | None, str, float]],
        kw_rows: list[tuple[int, str | None, str, float]],
        k: int,
    ) -> list[tuple[str | None, str, float]]:
        """Reciprocal-rank fusion. Returns ``(source, text, -rrf)`` best first."""
        scores: dict[int, float] = {}
        rows_by_id: dict[int, tuple[str | None, str]] = {}
        for rank_list in (vec_rows, kw_rows):
            for rank, (doc_id, source, text, _score) in enumerate(rank_list):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                rows_by_id.setdefault(doc_id, (source, text))
        ordered = sorted(scores.items(), key=lambda item: -item[1])[:k]
        return [
            (rows_by_id[doc_id][0], rows_by_id[doc_id][1], -score)
            for doc_id, score in ordered
        ]

    def count_documents(self, *, collection: str | None = "default") -> int:
        """Number of indexed document chunks. ``collection=None`` counts all."""
        if not (self._available or self._fts_available):
            return 0
        try:
            conn = self._connection()
            if collection is None:
                row = conn.execute("SELECT count(*) FROM documents_data").fetchone()
            else:
                row = conn.execute(
                    "SELECT count(*) FROM documents_data WHERE collection = ?",
                    (collection,),
                ).fetchone()
            return int(row[0])
        except Exception:
            logger.exception("VecStore: count_documents failed")
            return 0

    def clear_documents(self, *, collection: str | None = "default") -> None:
        """Delete indexed document chunks. ``collection=None`` clears all collections."""
        if not (self._available or self._fts_available):
            return
        try:
            conn = self._connection()
            if collection is None:
                if self._vec_loaded:
                    conn.execute("DELETE FROM documents_vec")
                conn.execute("DELETE FROM documents_data")
            else:
                # vec rows first, while the data rows still identify them.
                if self._vec_loaded:
                    conn.execute(
                        "DELETE FROM documents_vec WHERE rowid IN "
                        "(SELECT id FROM documents_data WHERE collection = ?)",
                        (collection,),
                    )
                conn.execute(
                    "DELETE FROM documents_data WHERE collection = ?", (collection,)
                )
            conn.commit()
        except Exception:
            logger.exception("VecStore: clear_documents failed")

    # ------------------------------------------------------------------
    # Meta + retrieval observability
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Read a value from the ``meta`` table (None when absent/unavailable)."""
        if not (self._available or self._fts_available):
            return None
        try:
            row = self._connection().execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        except Exception:
            logger.exception("VecStore: get_meta failed")
            return None

    def set_meta(self, key: str, value: str) -> None:
        """Write a key/value pair to the ``meta`` table."""
        if not (self._available or self._fts_available):
            return
        try:
            conn = self._connection()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        except Exception:
            logger.exception("VecStore: set_meta failed")

    def recent_retrievals(self, limit: int = 20) -> list[dict]:
        """Most recent retrieval-log rows (empty unless ``log_retrievals``)."""
        if not (self._available or self._fts_available):
            return []
        try:
            rows = self._connection().execute(
                "SELECT ts, store, caller, query, k, mode, collection, results"
                " FROM retrieval_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "ts": r[0], "store": r[1], "caller": r[2], "query": r[3],
                    "k": r[4], "mode": r[5], "collection": r[6],
                    "results": json.loads(r[7]) if r[7] else [],
                }
                for r in rows
            ]
        except Exception:
            logger.exception("VecStore: recent_retrievals failed")
            return []

    def _log_retrieval(
        self,
        store: str,
        caller: str | None,
        query: str,
        k: int,
        results: list[str],
        *,
        mode: str | None = None,
        collection: str | None = None,
    ) -> None:
        if not self._log_retrievals:
            return
        try:
            conn = self._connection()
            conn.execute(
                "INSERT INTO retrieval_log(ts, store, caller, query, k, mode,"
                " collection, results) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), store, caller, query, k, mode, collection,
                    json.dumps([r[:200] for r in results]),
                ),
            )
            conn.commit()
        except Exception:
            logger.exception("VecStore: retrieval logging failed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_init(self) -> bool:
        vec_importable = True
        embed_importable = True
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            vec_importable = False
        try:
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError:
            embed_importable = False
        if not (vec_importable and embed_importable):
            logger.info(
                "VecStore: sqlite-vec or sentence-transformers not installed; "
                "semantic search disabled (install moeka[vec] to enable; "
                "keyword search over documents stays available)"
            )
        try:
            conn = self._connection()
            self._ensure_schema(conn)
        except Exception:
            logger.exception("VecStore: failed to open vec.db")
            return False
        return vec_importable and embed_importable and self._vec_loaded

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                import sqlite_vec

                sqlite_vec.load(conn)
                self._vec_loaded = True
            except ImportError:
                self._vec_loaded = False
            self._conn = conn
        return self._conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._vec_loaded:
            dim = int(self.get_meta_raw(conn, "embedding_dim") or _EMBEDDING_DIM)
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS memory_chunks_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec
                    USING vec0(embedding FLOAT[{dim}]);

                CREATE TABLE IF NOT EXISTS history_entries_data (
                    id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS history_entries_vec
                    USING vec0(embedding FLOAT[{dim}]);

                CREATE TABLE IF NOT EXISTS skills_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS skills_vec
                    USING vec0(embedding FLOAT[{dim}]);
            """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                text TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT 'default'
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                store TEXT NOT NULL,
                caller TEXT,
                query TEXT NOT NULL,
                k INTEGER NOT NULL,
                mode TEXT,
                collection TEXT,
                results TEXT
            );
        """)
        if self._vec_loaded:
            dim = int(self.get_meta_raw(conn, "embedding_dim") or _EMBEDDING_DIM)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec"
                f" USING vec0(embedding FLOAT[{dim}])"
            )

        # Versioned column migrations (idempotent; ADD COLUMN with a constant
        # default is metadata-only, safe on live dbs).
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < _SCHEMA_VERSION:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(documents_data)")}
            if "collection" not in cols:
                conn.execute(
                    "ALTER TABLE documents_data "
                    "ADD COLUMN collection TEXT NOT NULL DEFAULT 'default'"
                )
            if "tags" not in cols:
                conn.execute("ALTER TABLE documents_data ADD COLUMN tags TEXT")
            if "created_at" not in cols:
                conn.execute("ALTER TABLE documents_data ADD COLUMN created_at TEXT")
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

        # FTS5 keyword index over documents, kept in sync by triggers.
        try:
            fts_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'documents_fts'"
            ).fetchone() is not None
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    text, content='documents_data', content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS documents_fts_ai
                AFTER INSERT ON documents_data BEGIN
                    INSERT INTO documents_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_fts_ad
                AFTER DELETE ON documents_data BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, text)
                        VALUES ('delete', old.id, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_fts_au
                AFTER UPDATE OF text ON documents_data BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, text)
                        VALUES ('delete', old.id, old.text);
                    INSERT INTO documents_fts(rowid, text) VALUES (new.id, new.text);
                END;
            """)
            # Backfill rows indexed before the FTS table existed. External-
            # content tables read through to documents_data on SELECT, so the
            # only reliable way to (re)build the actual index is the FTS5
            # 'rebuild' command — run it exactly once, on first creation.
            if not fts_existed:
                conn.execute(
                    "INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')"
                )
            self._fts_available = True
        except sqlite3.OperationalError:
            logger.info("VecStore: FTS5 unavailable; keyword search disabled")
            self._fts_available = False

        conn.commit()

    @staticmethod
    def get_meta_raw(conn: sqlite3.Connection, key: str) -> str | None:
        """Meta lookup usable during schema setup (tolerates a missing table)."""
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.debug("VecStore: loading embedding model {}", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        import numpy as np

        result = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        # encode() may return a single array when texts has one element
        if result.ndim == 1:
            vectors = [result.astype(np.float32)]
        else:
            vectors = [row.astype(np.float32) for row in result]
        if not self._embed_dim_checked and vectors:
            self._check_embedding_dim(len(vectors[0]))
        return vectors

    def _check_embedding_dim(self, dim: int) -> None:
        """Detect a model/dimension change and rebuild vec tables when needed.

        The ``meta`` table records which embedding model produced the stored
        vectors. On mismatch the vec tables are recreated at the new dimension
        and re-embedded from their text rows — embeddings are a rebuildable
        cache; the text is the source of truth.
        """
        self._embed_dim_checked = True
        try:
            conn = self._connection()
            stored_model = self.get_meta_raw(conn, "embedding_model")
            stored_dim = self.get_meta_raw(conn, "embedding_dim")
            if stored_model == self._model_name and stored_dim == str(dim):
                return
            if stored_model is None and stored_dim is None:
                # Fresh db (or first run after upgrade): record, and fix the
                # table dim if the default differs from the model's.
                if dim != self._table_dim(conn):
                    self._rebuild_vec_tables(conn, dim)
                self._record_embedding_meta(conn, dim)
                return
            logger.info(
                "VecStore: embedding model changed ({} -> {}); rebuilding vectors",
                stored_model, self._model_name,
            )
            self._rebuild_vec_tables(conn, dim)
            self._record_embedding_meta(conn, dim)
        except Exception:
            logger.exception("VecStore: embedding dimension check failed")

    def _table_dim(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'documents_vec'"
        ).fetchone()
        if row and row[0]:
            match = re.search(r"FLOAT\[(\d+)\]", row[0])
            if match:
                return int(match.group(1))
        return _EMBEDDING_DIM

    def _record_embedding_meta(self, conn: sqlite3.Connection, dim: int) -> None:
        for key, value in (("embedding_model", self._model_name), ("embedding_dim", str(dim))):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        conn.commit()

    def _rebuild_vec_tables(self, conn: sqlite3.Connection, dim: int) -> None:
        """Recreate all vec virtual tables at *dim* and re-embed stored text."""
        for store in _VEC_STORES:
            conn.execute(f"DROP TABLE IF EXISTS {store}_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE {store}_vec USING vec0(embedding FLOAT[{dim}])"
            )
        conn.commit()
        for store in _VEC_STORES:
            rows = conn.execute(f"SELECT id, text FROM {store}_data ORDER BY id").fetchall()
            if not rows:
                continue
            texts = [r[1] for r in rows]
            embeddings = self._embed(texts)
            for (row_id, _text), emb in zip(rows, embeddings):
                conn.execute(
                    f"INSERT INTO {store}_vec(rowid, embedding) VALUES (?, ?)",
                    (row_id, emb.tobytes()),
                )
            logger.info("VecStore: re-embedded {} row(s) in {}", len(rows), store)
        conn.commit()
