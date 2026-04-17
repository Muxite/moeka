"""Vector memory store using sqlite-vec for semantic search (Moeka extension).

This module provides :class:`VectorMemoryStore`, which wraps a :class:`MemoryStore`
and maintains a SQLite database (via ``sqlite-vec``) containing embeddings for:

- Long-term memory chunks (MEMORY.md, split on ``## `` section headings)
- Conversation history entries (history.jsonl)
- Skill descriptions

Embeddings are computed locally with *sentence-transformers* (all-MiniLM-L6-v2 by
default, 384 dimensions).  The model is loaded lazily on first use so startup is
not penalised if semantic search is never called.

Dependencies (optional extras):
    pip install "moeka[vec]"
    # or: pip install sqlite-vec sentence-transformers
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from nanobot.agent.memory import MemoryStore


@dataclass
class SearchResult:
    """A single result from a semantic memory search."""

    score: float
    source: str  # "memory", "history", or "skills"
    chunk_id: int
    text: str

    def format(self) -> str:
        bar = "#" * max(1, int(self.score * 20))
        return f"[{self.source}] (score={self.score:.3f}) {bar}\n{self.text}"


def _serialize_vec(v: list[float]) -> bytes:
    """Pack a float list into the little-endian float32 bytes sqlite-vec expects."""
    return struct.pack(f"{len(v)}f", *v)


class VectorMemoryStore:
    """Semantic search layer on top of :class:`MemoryStore`.

    Parameters
    ----------
    memory_store:
        The underlying file-based memory store to index.
    model_name:
        Sentence-transformers model identifier.
    top_k:
        Default number of results returned by :meth:`semantic_search`.
    chunk_size:
        Maximum character length of each MEMORY.md chunk.
    """

    _DB_NAME = "vec.db"
    _DIM = 384  # all-MiniLM-L6-v2 output dimension

    def __init__(
        self,
        memory_store: MemoryStore,
        *,
        model_name: str = "all-MiniLM-L6-v2",
        top_k: int = 5,
        chunk_size: int = 512,
    ) -> None:
        self._store = memory_store
        self._model_name = model_name
        self._top_k = top_k
        self._chunk_size = chunk_size
        self._db_path = memory_store.memory_dir / self._DB_NAME
        self._model = None  # loaded lazily
        self._conn = None  # opened lazily

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def semantic_search(
        self,
        query: str,
        k: int | None = None,
        scope: str = "all",
    ) -> list[SearchResult]:
        """Return the top-*k* most semantically similar chunks to *query*.

        Parameters
        ----------
        query:
            Natural-language search query.
        k:
            Number of results.  Defaults to the instance ``top_k``.
        scope:
            One of ``"all"``, ``"memory"``, ``"history"``, or ``"skills"``.
        """
        k = k or self._top_k
        try:
            self._ensure_ready()
            qvec = _serialize_vec(self._embed([query])[0])
            results: list[SearchResult] = []
            tables = self._tables_for_scope(scope)
            per_table = max(1, k // len(tables)) if tables else k
            for table, src_label in tables:
                rows = self._knn(table, qvec, per_table)
                for chunk_id, dist, text in rows:
                    score = max(0.0, 1.0 - dist)
                    results.append(SearchResult(score=score, source=src_label, chunk_id=chunk_id, text=text))
            results.sort(key=lambda r: r.score, reverse=True)
            return results[:k]
        except Exception as exc:
            logger.warning("VectorMemoryStore.semantic_search failed: {}", exc)
            return []

    def incremental_index(self) -> None:
        """Index any content that is not yet in the vector DB.

        Safe to call repeatedly — already-indexed content is skipped.
        """
        try:
            self._ensure_ready()
            self._index_memory()
            self._index_history()
            self._index_skills()
        except Exception as exc:
            logger.warning("VectorMemoryStore.incremental_index failed: {}", exc)

    def full_reindex(self) -> None:
        """Drop all existing vectors and rebuild from scratch."""
        try:
            self._ensure_ready()
            conn = self._conn
            conn.execute("DELETE FROM memory_chunks_data")
            conn.execute("DELETE FROM history_entries_data")
            conn.execute("DELETE FROM skills_data")
            conn.execute("DELETE FROM memory_chunks_vec")
            conn.execute("DELETE FROM history_entries_vec")
            conn.execute("DELETE FROM skills_vec")
            conn.commit()
            self.incremental_index()
        except Exception as exc:
            logger.warning("VectorMemoryStore.full_reindex failed: {}", exc)

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Open the DB and load the embedding model (both lazy)."""
        if self._conn is None:
            self._open_db()
        if self._model is None:
            self._load_model()

    def _load_model(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for vector memory. "
                "Install it with: pip install 'moeka[vec]'"
            ) from exc
        logger.info("Loading sentence-transformers model '{}'…", self._model_name)
        self._model = SentenceTransformer(self._model_name)

    def _open_db(self) -> None:
        try:
            import sqlite3
            import sqlite_vec
        except ImportError as exc:
            raise ImportError(
                "sqlite-vec is required for vector memory. "
                "Install it with: pip install 'moeka[vec]'"
            ) from exc

        conn = sqlite3.connect(str(self._db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables(conn)
        self._conn = conn

    def _create_tables(self, conn) -> None:
        dim = self._DIM
        # Data tables (hold the text)
        for tbl in ("memory_chunks", "history_entries", "skills"):
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {tbl}_data "
                f"(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)"
            )
        # Virtual vec tables (hold the embeddings, rowid matches _data.id)
        for tbl in ("memory_chunks", "history_entries", "skills"):
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {tbl}_vec "
                f"USING vec0(embedding FLOAT[{dim}])"
            )
        conn.commit()

    # ------------------------------------------------------------------
    # Embedding helpers
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return vecs.tolist()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _index_memory(self) -> None:
        """Chunk MEMORY.md and index any new chunks."""
        content = self._store.read_memory()
        if not content.strip():
            return
        chunks = self._chunk_memory(content)
        existing = self._count_rows("memory_chunks_data")
        new_chunks = chunks[existing:]
        if not new_chunks:
            return
        vecs = self._embed(new_chunks)
        for text, vec in zip(new_chunks, vecs):
            row_id = self._insert_data("memory_chunks", text)
            self._insert_vec("memory_chunks", row_id, vec)
        self._conn.commit()
        logger.debug("Indexed {} new MEMORY.md chunk(s)", len(new_chunks))

    def _index_history(self) -> None:
        """Index history.jsonl entries that aren't yet in the DB."""
        entries = self._store._read_entries()
        existing = self._count_rows("history_entries_data")
        new_entries = entries[existing:]
        if not new_entries:
            return
        texts = [e.get("content", "") for e in new_entries]
        vecs = self._embed(texts)
        for text, vec in zip(texts, vecs):
            row_id = self._insert_data("history_entries", text)
            self._insert_vec("history_entries", row_id, vec)
        self._conn.commit()
        logger.debug("Indexed {} new history entry/entries", len(new_entries))

    def _index_skills(self) -> None:
        """Index skill descriptions.  Full replace on count mismatch."""
        skills = self._load_skill_texts()
        existing = self._count_rows("skills_data")
        if existing == len(skills):
            return
        # Skills change rarely; re-index all on mismatch.
        self._conn.execute("DELETE FROM skills_data")
        self._conn.execute("DELETE FROM skills_vec")
        if not skills:
            self._conn.commit()
            return
        vecs = self._embed(skills)
        for text, vec in zip(skills, vecs):
            row_id = self._insert_data("skills", text)
            self._insert_vec("skills", row_id, vec)
        self._conn.commit()
        logger.debug("Indexed {} skill(s)", len(skills))

    # ------------------------------------------------------------------
    # KNN query
    # ------------------------------------------------------------------

    def _knn(self, table: str, qvec: bytes, k: int) -> list[tuple[int, float, str]]:
        """Return (chunk_id, distance, text) from a vec0 KNN scan."""
        sql = (
            f"SELECT v.rowid, v.distance, d.text "
            f"FROM {table}_vec v JOIN {table}_data d ON v.rowid = d.id "
            f"WHERE v.embedding MATCH ? AND k = ?"
        )
        rows = self._conn.execute(sql, (qvec, k)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ------------------------------------------------------------------
    # Low-level DB helpers
    # ------------------------------------------------------------------

    def _count_rows(self, table: str) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _insert_data(self, table: str, text: str) -> int:
        cur = self._conn.execute(f"INSERT INTO {table}_data (text) VALUES (?)", (text,))
        return cur.lastrowid

    def _insert_vec(self, table: str, row_id: int, vec: list[float]) -> None:
        self._conn.execute(
            f"INSERT INTO {table}_vec (rowid, embedding) VALUES (?, ?)",
            (row_id, _serialize_vec(vec)),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _chunk_memory(self, content: str) -> list[str]:
        """Split MEMORY.md on ``## `` section headings, then by chunk_size."""
        sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)
        chunks: list[str] = []
        for section in sections:
            section = section.strip()
            if not section:
                continue
            # If section fits within chunk_size, keep as-is
            if len(section) <= self._chunk_size:
                chunks.append(section)
            else:
                # Sliding-window split
                for i in range(0, len(section), self._chunk_size):
                    piece = section[i : i + self._chunk_size].strip()
                    if piece:
                        chunks.append(piece)
        return chunks

    def _load_skill_texts(self) -> list[str]:
        """Return a list of 'skill_name: description' strings from the workspace."""
        skill_dir = self._store.workspace / "skills"
        texts: list[str] = []
        if not skill_dir.is_dir():
            # Try built-in skills directory
            from nanobot.agent.skills import BUILTIN_SKILLS_DIR
            skill_dir = BUILTIN_SKILLS_DIR
        for skill_md in sorted(skill_dir.glob("*/SKILL.md")):
            try:
                content = skill_md.read_text(encoding="utf-8").strip()
                name = skill_md.parent.name
                # First non-empty line is usually the title
                first_line = next((l.lstrip("# ").strip() for l in content.splitlines() if l.strip()), name)
                texts.append(f"{name}: {first_line}")
            except OSError:
                continue
        return texts

    @staticmethod
    def _tables_for_scope(scope: str) -> list[tuple[str, str]]:
        mapping = {
            "memory": [("memory_chunks", "memory")],
            "history": [("history_entries", "history")],
            "skills": [("skills", "skills")],
            "all": [
                ("memory_chunks", "memory"),
                ("history_entries", "history"),
                ("skills", "skills"),
            ],
        }
        return mapping.get(scope, mapping["all"])

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
