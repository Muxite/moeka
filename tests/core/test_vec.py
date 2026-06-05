"""Tests for nanobot.core.vec — the loop-less embeddings entrypoint.

Pins the awork integration contract: ``open_vec_store`` must hand back a
working VecStore with no AgentLoop, provider, config, or API key involved,
and degrade to an inert store (never raise) when the vec extras are missing.
"""

from __future__ import annotations

import dataclasses

import pytest

from nanobot.core.vec import RetrievedChunk, open_vec_store
from nanobot.core.vec_store import VecStore


def test_open_vec_store_returns_vec_store(tmp_path):
    store = open_vec_store(tmp_path / "vec.db")
    assert isinstance(store, VecStore)


def test_open_vec_store_accepts_str_path(tmp_path):
    store = open_vec_store(str(tmp_path / "vec.db"))
    assert isinstance(store, VecStore)


def test_open_vec_store_model_none_uses_default(tmp_path):
    store = open_vec_store(tmp_path / "vec.db", model=None)
    assert store._model_name == "all-MiniLM-L6-v2"


def test_open_vec_store_model_override(tmp_path):
    store = open_vec_store(tmp_path / "vec.db", model="custom-model")
    assert store._model_name == "custom-model"


def test_degraded_store_is_inert_not_raising(tmp_path, monkeypatch):
    """Without vec extras every documents method returns a benign value."""
    monkeypatch.setattr(VecStore, "_try_init", lambda self: False)
    store = open_vec_store(tmp_path / "vec.db")
    assert store.available is False
    assert store.add_documents("some text", source="s") == 0
    assert store.search_documents("query") == []
    assert store.search_documents_scored("query") == []
    assert store.count_documents() == 0
    assert store.count_documents(collection=None) == 0
    store.clear_documents()  # must not raise
    store.clear_documents(collection=None)


def test_retrieved_chunk_is_frozen():
    chunk = RetrievedChunk(text="t", source="s", score=0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.text = "changed"


def test_retrieved_chunk_fields():
    chunk = RetrievedChunk(text="body", source=None, score=1.25)
    assert chunk.text == "body"
    assert chunk.source is None
    assert chunk.score == 1.25


def test_core_package_reexports():
    """Hosts import from nanobot.core directly — keep the surface stable."""
    import nanobot.core as core_pkg

    assert core_pkg.RetrievedChunk is RetrievedChunk
    assert core_pkg.open_vec_store is open_vec_store
