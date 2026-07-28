"""Real-embedding tests for the VecStore documents API (scored search,
collections, migration).

These need ``moeka[vec]`` (sqlite-vec + sentence-transformers); they skip
otherwise. In docker they run when the vec extra is included
(``--build-arg NO_EXTRA=``). Keyless-degradation twins live in
tests/core/test_vec.py and run everywhere.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("sentence_transformers")

from nanobot.core.vec_store import VecStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = VecStore(tmp_path / "vec.db")
    if not s.available:
        pytest.skip("vec backend unavailable")
    return s


def test_scored_search_returns_source_text_distance(store):
    store.add_documents("The Eiffel Tower is in Paris, France.", source="facts.md")
    store.add_documents("Asyncio schedules coroutines on an event loop.", source="dev.md")

    results = store.search_documents_scored("Where is the Eiffel Tower?", k=2)
    assert len(results) == 2
    source, text, score = results[0]
    assert source == "facts.md"
    assert "Paris" in text
    assert isinstance(score, float)
    # Ordered by ascending distance (closest first).
    assert results[0][2] <= results[1][2]


def test_scored_search_source_can_be_none(store):
    store.add_documents("anonymous knowledge with no source")
    [(source, text, _score)] = store.search_documents_scored("anonymous knowledge", k=1)
    assert source is None
    assert "anonymous" in text


def test_search_documents_delegates_to_scored(store):
    store.add_documents("The capital of Japan is Tokyo.", source="geo")
    hits = store.search_documents("capital of Japan", k=1)
    assert hits == [r[1] for r in store.search_documents_scored("capital of Japan", k=1)]


def test_collection_isolation(store):
    store.add_documents("Kubernetes orchestrates containers.", source="k8s", collection="infra")
    store.add_documents("Sourdough needs a mature starter.", source="bread", collection="baking")

    infra = store.search_documents_scored("container orchestration", k=5, collection="infra")
    assert {r[0] for r in infra} == {"k8s"}
    baking = store.search_documents_scored("bread starter", k=5, collection="baking")
    assert {r[0] for r in baking} == {"bread"}
    # collection=None searches across all.
    all_hits = store.search_documents_scored("containers and bread", k=5, collection=None)
    assert {r[0] for r in all_hits} == {"k8s", "bread"}
    # Default collection holds neither.
    assert store.search_documents_scored("anything", k=5) == []


def test_count_and_clear_scoped_by_collection(store):
    store.add_documents("default doc one")
    store.add_documents("job posting text", collection="jobs")
    store.add_documents("another job text", collection="jobs")

    assert store.count_documents() >= 1  # default
    assert store.count_documents(collection="jobs") >= 2
    total = store.count_documents(collection=None)
    assert total == store.count_documents() + store.count_documents(collection="jobs")

    store.clear_documents(collection="jobs")
    assert store.count_documents(collection="jobs") == 0
    assert store.count_documents() >= 1  # default untouched

    store.clear_documents(collection=None)
    assert store.count_documents(collection=None) == 0


def test_clear_removes_vectors_not_just_rows(store):
    store.add_documents("ephemeral content", collection="tmp")
    store.clear_documents(collection="tmp")
    conn = store._connection()
    assert conn.execute("SELECT count(*) FROM documents_vec").fetchone()[0] == 0


def test_mixed_collections_overfetch_returns_full_k(store):
    """A small collection must not be starved by a large neighbour.

    The collection predicate filters after vec0's KNN; without over-fetch a
    query for k results from the minority collection could come back short.
    """
    for i in range(20):
        store.add_documents(f"filler document number {i} about misc topics", collection="big")
    store.add_documents("the lone target document about quantum physics", collection="small")

    hits = store.search_documents_scored("quantum physics", k=1, collection="small")
    assert len(hits) == 1
    assert "quantum" in hits[0][1]


def test_migration_adds_collection_column_to_legacy_db(tmp_path):
    """Opening a pre-collections vec.db migrates it in place."""
    import sqlite_vec

    db = tmp_path / "vec.db"
    conn = sqlite3.connect(str(db))
    sqlite_vec.load(conn)
    conn.executescript("""
        CREATE TABLE memory_chunks_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE memory_chunks_vec USING vec0(embedding FLOAT[384]);
        CREATE TABLE history_entries_data (id INTEGER PRIMARY KEY, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE history_entries_vec USING vec0(embedding FLOAT[384]);
        CREATE TABLE skills_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE skills_vec USING vec0(embedding FLOAT[384]);
        CREATE TABLE documents_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE documents_vec USING vec0(embedding FLOAT[384]);
    """)
    conn.execute(
        "INSERT INTO documents_data(source, text) VALUES ('legacy.md', 'pre-collections row')"
    )
    conn.commit()
    conn.close()

    store = VecStore(db)
    if not store.available:
        pytest.skip("vec backend unavailable")

    cols = {r[1] for r in store._connection().execute("PRAGMA table_info(documents_data)")}
    assert "collection" in cols
    # Legacy rows land in 'default' and stay countable/clearable.
    assert store.count_documents() == 1
    assert store.count_documents(collection=None) == 1

    # Re-opening is idempotent (no duplicate-column error).
    store2 = VecStore(db)
    assert store2.available
    assert store2.count_documents() == 1


def test_add_documents_returns_chunk_count(store):
    text = "\n\n".join(f"Paragraph {i}: " + "content " * 40 for i in range(4))
    n = store.add_documents(text, source="long.md")
    assert n >= 2
    assert store.count_documents() == n
