"""Tests for the host-documents API additions: scored retrieval, ingest_text,
clear/count, and the loop-less ``open_vec_store`` constructor."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")
pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")


_CONFIG = {
    "providers": {"openrouter": {"apiKey": "sk-test-key"}},
    "agents": {"defaults": {"vec": {"enable": True}}},
}


def _store(tmp_path):
    from nanobot.core.vec import open_vec_store

    return open_vec_store(tmp_path / "vec.db")


def test_open_vec_store_is_loop_less(tmp_path):
    store = _store(tmp_path)
    assert store.available


def test_search_documents_scored_returns_source_and_score(tmp_path):
    store = _store(tmp_path)
    store.add_documents("Kubernetes clusters run on three nodes.", source="infra.md")
    store.add_documents("The resume lists five Python projects.", source="resume.md")

    results = store.search_documents_scored("container orchestration cluster", k=2)
    assert results
    sources = {source for source, _text, _score in results}
    assert sources <= {"infra.md", "resume.md"}
    # closest hit should be the infra doc, with a finite distance
    source, text, score = results[0]
    assert source == "infra.md"
    assert "Kubernetes" in text
    assert isinstance(score, float)
    # results are ordered by distance ascending
    scores = [s for _, _, s in results]
    assert scores == sorted(scores)


def test_search_documents_matches_scored_texts(tmp_path):
    store = _store(tmp_path)
    store.add_documents("Disk usage on /var reached 80 percent.", source="ops")
    bare = store.search_documents("storage capacity", k=3)
    scored = store.search_documents_scored("storage capacity", k=3)
    assert bare == [text for _src, text, _score in scored]


def test_ingest_text_never_path_detects(tmp_path):
    """A short string that names an existing file must be ingested verbatim."""
    from nanobot.core import MoekaCore

    target = tmp_path / "secret.txt"
    target.write_text("file contents that must NOT be ingested")

    core = MoekaCore.create(config_dict=dict(_CONFIG), workspace=tmp_path / "ws")
    try:
        if not core.vec_available:
            pytest.skip("vec store unavailable")
        count = core.ingest_text(str(target), source="literal")
        assert count >= 1
        hits = core.retrieve_documents(str(target), k=1)
        assert hits and str(target) in hits[0].text
        assert hits[0].source == "literal"
        assert all("must NOT be ingested" not in h.text for h in hits)
    finally:
        core.cleanup()


def test_core_retrieve_documents_and_counts(tmp_path):
    from nanobot.core import MoekaCore, RetrievedChunk

    core = MoekaCore.create(config_dict=dict(_CONFIG), workspace=tmp_path / "ws")
    try:
        if not core.vec_available:
            pytest.skip("vec store unavailable")
        assert core.count_documents() == 0
        core.ingest_text(
            "Project X ships on Friday.\n\nThe launch checklist has nine items.",
            source="notes",
        )
        assert core.count_documents() >= 1

        chunks = core.retrieve_documents("when does the project ship", k=3)
        assert chunks
        assert isinstance(chunks[0], RetrievedChunk)
        assert chunks[0].source == "notes"

        core.clear_documents()
        assert core.count_documents() == 0
        assert core.retrieve_documents("ship", k=3) == []
    finally:
        core.cleanup()


def test_clear_documents_leaves_memory_alone(tmp_path):
    store = _store(tmp_path)
    store.upsert_memory_chunks("## Fact\nMoeka manages Docker containers.")
    store.add_documents("Host document text.", source="doc")
    store.clear_documents()
    assert store.count_documents() == 0
    assert store.search_memory("Docker", k=5)


def test_unavailable_store_is_inert(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "_available", False)
    assert store.search_documents_scored("anything") == []
    assert store.count_documents() == 0
    store.clear_documents()  # must not raise
