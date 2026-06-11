"""Tests for VecStore metadata filters, FTS5 keyword/hybrid search, the
retrieval log, and the embedding-model rebuild path.

Real-embedding tests skip without ``moeka[vec]``; the keyword-only tests run
everywhere (FTS5 ships with stock sqlite3).
"""

from __future__ import annotations

import sqlite3

import pytest

from nanobot.core.vec_store import VecStore

_HAS_VEC = True
try:
    import sentence_transformers  # noqa: F401
    import sqlite_vec  # noqa: F401
except ImportError:
    _HAS_VEC = False

needs_vec = pytest.mark.skipif(not _HAS_VEC, reason="moeka[vec] not installed")


@pytest.fixture
def store(tmp_path):
    return VecStore(tmp_path / "vec.db")


# ---------------------------------------------------------------------------
# Keyword (FTS5) search — works with or without embeddings
# ---------------------------------------------------------------------------

def test_keyword_search_finds_exact_terms(store):
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    store.add_documents("The Eiffel Tower is in Paris, France.", source="facts.md")
    store.add_documents("Asyncio schedules coroutines on an event loop.", source="dev.md")
    results = store.search_documents_scored("Eiffel Tower", k=2, mode="keyword")
    assert results
    assert "Eiffel" in results[0][1]


def test_keyword_search_empty_for_no_match(store):
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    store.add_documents("alpha beta gamma", source="a")
    assert store.search_documents("zzzznothing", k=3, mode="keyword") == []


def test_unknown_mode_raises(store):
    with pytest.raises(ValueError):
        store.search_documents("q", mode="nonsense")


# ---------------------------------------------------------------------------
# Metadata: tags + since filters
# ---------------------------------------------------------------------------

def test_tags_roundtrip_and_filter_keyword(store):
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    store.add_documents("Acme ships rockets weekly.", source="acme",
                        tags=["company:acme", "research"])
    store.add_documents("Globex builds doomsday devices.", source="globex",
                        tags=["company:globex", "research"])
    hits = store.search_documents(
        "ships rockets devices", k=5, mode="keyword", tags=["company:acme"],
    )
    assert len(hits) == 1
    assert "Acme" in hits[0]


def test_since_filter_excludes_old_rows(store):
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    store.add_documents("ancient knowledge artifact", source="old")
    # Backdate the row we just inserted.
    conn = store._connection()
    conn.execute("UPDATE documents_data SET created_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    store.add_documents("fresh knowledge artifact", source="new")
    hits = store.search_documents(
        "knowledge artifact", k=5, mode="keyword", since="2020-01-01",
    )
    assert len(hits) == 1
    assert "fresh" in hits[0]


# ---------------------------------------------------------------------------
# Hybrid (RRF) search
# ---------------------------------------------------------------------------

@needs_vec
def test_hybrid_fuses_vec_and_keyword(store):
    if not (store.available and store.keyword_available):
        pytest.skip("needs both vec and FTS5")
    store.add_documents("The Eiffel Tower is in Paris, France.", source="facts.md")
    store.add_documents("Sourdough bread needs a mature starter.", source="bread.md")
    results = store.search_documents_scored(
        "Where is the Eiffel Tower located?", k=2, mode="hybrid",
    )
    assert results
    assert "Paris" in results[0][1]
    # Best-first ordering: scores ascend.
    scores = [r[2] for r in results]
    assert scores == sorted(scores)


def test_hybrid_degrades_to_keyword_without_embeddings(store, monkeypatch):
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    store.add_documents("Kubernetes orchestrates containers", source="k8s")
    monkeypatch.setattr(store, "_available", False)
    hits = store.search_documents("Kubernetes containers", k=2, mode="hybrid")
    assert hits and "Kubernetes" in hits[0]


def test_keyword_only_store_indexes_without_embeddings(tmp_path, monkeypatch):
    """Documents added while embeddings are down are still keyword-searchable."""
    s = VecStore(tmp_path / "kw.db")
    if not s.keyword_available:
        pytest.skip("FTS5 unavailable")
    monkeypatch.setattr(s, "_available", False)
    added = s.add_documents("graceful degradation is a feature", source="notes")
    assert added > 0
    assert s.count_documents() == added
    hits = s.search_documents("graceful degradation", k=1, mode="keyword")
    assert hits


# ---------------------------------------------------------------------------
# Retrieval log
# ---------------------------------------------------------------------------

def test_retrieval_log_records_searches(tmp_path):
    s = VecStore(tmp_path / "vec.db", log_retrievals=True)
    if not s.keyword_available:
        pytest.skip("FTS5 unavailable")
    s.add_documents("observable retrieval pipeline", source="obs")
    s.search_documents("observable pipeline", k=3, mode="keyword", caller="test-caller")
    log = s.recent_retrievals()
    assert log
    entry = log[0]
    assert entry["store"] == "documents"
    assert entry["caller"] == "test-caller"
    assert entry["query"] == "observable pipeline"
    assert entry["mode"] == "keyword"
    assert any("observable" in r for r in entry["results"])


def test_retrieval_log_off_by_default(tmp_path):
    s = VecStore(tmp_path / "vec.db")
    if not s.keyword_available:
        pytest.skip("FTS5 unavailable")
    s.add_documents("quiet by default", source="q")
    s.search_documents("quiet", k=1, mode="keyword")
    assert s.recent_retrievals() == []


# ---------------------------------------------------------------------------
# Meta table + embedding model rebuild
# ---------------------------------------------------------------------------

def test_meta_roundtrip(store):
    if not (store.available or store.keyword_available):
        pytest.skip("store unavailable")
    store.set_meta("corpus_sig", "abc123")
    assert store.get_meta("corpus_sig") == "abc123"
    store.set_meta("corpus_sig", "def456")
    assert store.get_meta("corpus_sig") == "def456"
    assert store.get_meta("missing") is None


@needs_vec
def test_embedding_meta_recorded_on_first_embed(store):
    if not store.available:
        pytest.skip("vec backend unavailable")
    store.add_documents("record the model, please", source="m")
    assert store.get_meta("embedding_model") == "all-MiniLM-L6-v2"
    assert store.get_meta("embedding_dim") == "384"


@needs_vec
def test_model_change_triggers_rebuild(tmp_path):
    """Reopening with a different model name re-embeds from the text rows."""
    s1 = VecStore(tmp_path / "vec.db")
    if not s1.available:
        pytest.skip("vec backend unavailable")
    s1.add_documents("The Eiffel Tower is in Paris.", source="facts")
    assert s1.get_meta("embedding_model") == "all-MiniLM-L6-v2"
    s1._conn.close()

    # Same underlying weights, different registry name — forces the
    # mismatch path without downloading a second model.
    s2 = VecStore(
        tmp_path / "vec.db",
        model_name="sentence-transformers/all-MiniLM-L6-v2",
    )
    assert s2.available
    hits = s2.search_documents("Where is the Eiffel Tower?", k=1)
    assert hits and "Paris" in hits[0]
    assert s2.get_meta("embedding_model") == "sentence-transformers/all-MiniLM-L6-v2"
    # Vector rows survived the rebuild.
    conn = sqlite3.connect(tmp_path / "vec.db")
    n_data = conn.execute("SELECT count(*) FROM documents_data").fetchone()[0]
    assert n_data >= 1
    conn.close()


def test_legacy_db_migrates_columns(tmp_path):
    """A pre-metadata documents_data table gains tags/created_at on open."""
    db = tmp_path / "vec.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE documents_data ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, text TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO documents_data(source, text) VALUES ('old', 'legacy row')")
    conn.commit()
    conn.close()

    s = VecStore(db)
    if not (s.available or s.keyword_available):
        pytest.skip("store unavailable")
    cols = {row[1] for row in s._connection().execute("PRAGMA table_info(documents_data)")}
    assert {"collection", "tags", "created_at"} <= cols
    # Legacy row is keyword-searchable after the FTS backfill.
    if s.keyword_available:
        assert s.search_documents("legacy row", k=1, mode="keyword", collection=None)
