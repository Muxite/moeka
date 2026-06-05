"""Semantic vector store backed by sqlite-vec + sentence-transformers.

Provides four stores:
  - memory_chunks   — MEMORY.md split by section headers
  - history_entries — history.jsonl entries
  - skills          — skill definitions (indexed at startup)
  - documents       — host-supplied text (append-only), optionally split
                      into named *collections* so a host can keep separate
                      corpora in one vec.db

All public methods degrade gracefully: if sqlite-vec or
sentence-transformers are unavailable the caller gets None/[] back.
Import this module safely; it never raises at import time.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import numpy as np

_SECTION_RE = re.compile(r"^#{1,3} .+", re.MULTILINE)
_MAX_CHUNK_CHARS = 500
_EMBEDDING_DIM = 384


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


class VecStore:
    """sqlite-vec backed semantic store for memory, history, and skills."""

    def __init__(self, db_path: Path, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._db_path = db_path
        self._model_name = model_name
        self._model = None  # lazy-loaded
        self._conn: sqlite3.Connection | None = None
        self._available = self._try_init()

    # ------------------------------------------------------------------
    # Public availability check
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

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

    def search_memory(self, query: str, k: int = 5) -> list[str]:
        """Return the top-k memory chunks semantically closest to *query*."""
        if not self._available or not query.strip():
            return []
        try:
            conn = self._connection()
            count = conn.execute("SELECT count(*) FROM memory_chunks_data").fetchone()[0]
            if count == 0:
                return []
            emb = self._embed([query])[0]
            limit = min(k, count)
            rows = conn.execute(
                """
                SELECT d.text
                FROM memory_chunks_vec v
                JOIN memory_chunks_data d ON d.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (emb.tobytes(), limit),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            logger.exception("VecStore: search_memory failed")
            return []

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

    def search_history(self, query: str, k: int = 5) -> list[str]:
        """Return the top-k history entries semantically closest to *query*."""
        if not self._available or not query.strip():
            return []
        try:
            conn = self._connection()
            count = conn.execute("SELECT count(*) FROM history_entries_data").fetchone()[0]
            if count == 0:
                return []
            emb = self._embed([query])[0]
            limit = min(k, count)
            rows = conn.execute(
                """
                SELECT d.text
                FROM history_entries_vec v
                JOIN history_entries_data d ON d.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (emb.tobytes(), limit),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            logger.exception("VecStore: search_history failed")
            return []

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
            for (name, text), emb in zip(skills, embeddings):
                cur = conn.execute(
                    "INSERT INTO skills_data(text) VALUES (?)", (f"{name}: {text}",)
                )
                conn.execute(
                    "INSERT INTO skills_vec(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, emb.tobytes()),
                )
            conn.commit()
            logger.debug("VecStore: indexed {} skill(s)", len(skills))
        except Exception:
            logger.exception("VecStore: upsert_skills failed")

    def search_skills(self, query: str, k: int = 5) -> list[str]:
        """Return the top-k skill texts semantically closest to *query*."""
        if not self._available or not query.strip():
            return []
        try:
            conn = self._connection()
            count = conn.execute("SELECT count(*) FROM skills_data").fetchone()[0]
            if count == 0:
                return []
            emb = self._embed([query])[0]
            limit = min(k, count)
            rows = conn.execute(
                """
                SELECT d.text
                FROM skills_vec v
                JOIN skills_data d ON d.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (emb.tobytes(), limit),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            logger.exception("VecStore: search_skills failed")
            return []

    # ------------------------------------------------------------------
    # Host documents (incremental, append-only)
    # ------------------------------------------------------------------

    def add_documents(
        self, text: str, source: str | None = None, *, collection: str = "default"
    ) -> int:
        """Chunk and index host-supplied *text* incrementally. Returns chunk count."""
        if not self._available or not text.strip():
            return 0
        chunks = _chunk_document(text)
        if not chunks:
            return 0
        try:
            conn = self._connection()
            embeddings = self._embed(chunks)
            for chunk, emb in zip(chunks, embeddings):
                cur = conn.execute(
                    "INSERT INTO documents_data(source, text, collection) VALUES (?, ?, ?)",
                    (source, chunk, collection),
                )
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
        self, query: str, k: int = 5, *, collection: str | None = "default"
    ) -> list[str]:
        """Return the top-k host-document chunks semantically closest to *query*."""
        return [
            text for _source, text, _score in self.search_documents_scored(
                query, k=k, collection=collection
            )
        ]

    def search_documents_scored(
        self, query: str, k: int = 5, *, collection: str | None = "default"
    ) -> list[tuple[str | None, str, float]]:
        """Top-k host-document chunks as ``(source, text, distance)``.

        Lower distance = semantically closer (embeddings are unit-normalized).
        ``collection=None`` searches across all collections.
        """
        if not self._available or not query.strip():
            return []
        try:
            conn = self._connection()
            total = conn.execute("SELECT count(*) FROM documents_data").fetchone()[0]
            if total == 0:
                return []
            in_scope = total
            if collection is not None:
                in_scope = conn.execute(
                    "SELECT count(*) FROM documents_data WHERE collection = ?",
                    (collection,),
                ).fetchone()[0]
                if in_scope == 0:
                    return []
            emb = self._embed([query])[0]
            want = min(k, in_scope)
            # The collection predicate filters *after* the KNN returns its k
            # nearest rows, so with mixed collections a plain k would
            # under-return. Over-fetch, escalating to a full scan if needed
            # (vec0 brute-forces either way; only row materialization grows).
            knn_k = want if collection is None or in_scope == total else min(
                total, max(want * 4, 32)
            )
            while True:
                rows = self._knn_documents(conn, emb, knn_k, collection)
                if len(rows) >= want or knn_k >= total:
                    return rows[:want]
                knn_k = total

        except Exception:
            logger.exception("VecStore: search_documents_scored failed")
            return []

    def _knn_documents(
        self,
        conn: sqlite3.Connection,
        emb: np.ndarray,
        knn_k: int,
        collection: str | None,
    ) -> list[tuple[str | None, str, float]]:
        sql = """
            SELECT d.source, d.text, v.distance
            FROM documents_vec v
            JOIN documents_data d ON d.id = v.rowid
            WHERE v.embedding MATCH ?
              AND k = ?
        """
        params: list = [emb.tobytes(), knn_k]
        if collection is not None:
            sql += " AND d.collection = ?"
            params.append(collection)
        sql += " ORDER BY distance"
        rows = conn.execute(sql, params).fetchall()
        return [(r[0], r[1], float(r[2])) for r in rows]

    def count_documents(self, *, collection: str | None = "default") -> int:
        """Number of indexed document chunks. ``collection=None`` counts all."""
        if not self._available:
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
        if not self._available:
            return
        try:
            conn = self._connection()
            if collection is None:
                conn.execute("DELETE FROM documents_vec")
                conn.execute("DELETE FROM documents_data")
            else:
                # vec rows first, while the data rows still identify them.
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_init(self) -> bool:
        try:
            import sqlite_vec  # noqa: F401
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError:
            logger.info(
                "VecStore: sqlite-vec or sentence-transformers not installed; "
                "semantic search disabled (install moeka[vec] to enable)"
            )
            return False
        try:
            conn = self._connection()
            self._ensure_schema(conn)
            return True
        except Exception:
            logger.exception("VecStore: failed to open vec.db")
            return False

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            import sqlite_vec

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            sqlite_vec.load(conn)
            self._conn = conn
        return self._conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS memory_chunks_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec
                USING vec0(embedding FLOAT[{_EMBEDDING_DIM}]);

            CREATE TABLE IF NOT EXISTS history_entries_data (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS history_entries_vec
                USING vec0(embedding FLOAT[{_EMBEDDING_DIM}]);

            CREATE TABLE IF NOT EXISTS skills_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_vec
                USING vec0(embedding FLOAT[{_EMBEDDING_DIM}]);

            CREATE TABLE IF NOT EXISTS documents_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                text TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT 'default'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec
                USING vec0(embedding FLOAT[{_EMBEDDING_DIM}]);
        """)
        # Migrate pre-collections documents_data in place (idempotent; ADD
        # COLUMN with a constant default is metadata-only, safe on live dbs).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents_data)")}
        if "collection" not in cols:
            conn.execute(
                "ALTER TABLE documents_data "
                "ADD COLUMN collection TEXT NOT NULL DEFAULT 'default'"
            )
        conn.commit()

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.debug("VecStore: loading embedding model {}", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        import numpy as np

        result = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        # encode() may return a single array when texts has one element
        if result.ndim == 1:
            return [result.astype(np.float32)]
        return [row.astype(np.float32) for row in result]
