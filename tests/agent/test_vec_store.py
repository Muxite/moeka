"""Tests for VecStore semantic memory."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")
pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")


def test_vec_store_available(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    assert store.available


def test_memory_chunks_upsert_and_search(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    text = "## Docker\nMoeka manages Docker containers on the host.\n\n## Kubernetes\nKubernetes clusters run on three nodes."
    store.upsert_memory_chunks(text)

    results = store.search_memory("container orchestration", k=5)
    assert len(results) >= 1
    assert any("Docker" in r or "Kubernetes" in r for r in results)


def test_memory_chunks_full_replace(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    store.upsert_memory_chunks("## OldFact\nThis should be gone.")
    store.upsert_memory_chunks("## NewFact\nOnly this survives.")

    results = store.search_memory("OldFact", k=10)
    assert all("OldFact" not in r for r in results)
    results2 = store.search_memory("NewFact", k=10)
    assert any("NewFact" in r for r in results2)


def test_history_entry_upsert_and_search(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    store.upsert_history_entry(1, "User asked about disk space on the server.")
    store.upsert_history_entry(2, "Agent reported 80% disk usage on /var.")
    store.upsert_history_entry(3, "User asked about weather in Bangkok.")

    results = store.search_history("storage capacity", k=10)
    assert len(results) >= 1
    assert any("disk" in r.lower() for r in results)


def test_history_entry_dedup(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    store.upsert_history_entry(1, "Test entry.")
    store.upsert_history_entry(1, "Test entry.")  # same cursor — should not duplicate

    conn = store._connection()
    count = conn.execute("SELECT count(*) FROM history_entries_data WHERE id = 1").fetchone()[0]
    assert count == 1


def test_skills_upsert_and_search(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    store.upsert_skills([
        ("docker", "Manage Docker containers, images, and volumes on the host."),
        ("weather", "Fetch current weather and forecasts from wttr.in."),
        ("github", "Interact with GitHub repositories via gh CLI."),
    ])

    results = store.search_skills("show container logs", k=5)
    assert len(results) >= 1
    assert any("docker" in r.lower() for r in results)


def test_search_empty_db_returns_empty(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    assert store.search_memory("anything") == []
    assert store.search_history("anything") == []
    assert store.search_skills("anything") == []


def test_returns_k_or_fewer_results(tmp_path):
    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    store.upsert_memory_chunks("## A\nfirst\n\n## B\nsecond")
    results = store.search_memory("content", k=10)
    assert len(results) <= 10


def test_graceful_import_error(monkeypatch, tmp_path):
    """VecStore.available is False when dependencies are missing."""
    import importlib
    import sys

    # Hide sqlite_vec so _try_init fails
    real_sqlite_vec = sys.modules.get("sqlite_vec")
    sys.modules["sqlite_vec"] = None  # type: ignore[assignment]
    try:
        # Need to reload vec_store to re-run _try_init with patched imports
        if "nanobot.agent.vec_store" in sys.modules:
            del sys.modules["nanobot.agent.vec_store"]
        from nanobot.agent.vec_store import VecStore

        store = VecStore(tmp_path / "vec.db")
        assert not store.available
        assert store.search_memory("test") == []
    finally:
        if real_sqlite_vec is not None:
            sys.modules["sqlite_vec"] = real_sqlite_vec
        elif "sqlite_vec" in sys.modules:
            del sys.modules["sqlite_vec"]
        if "nanobot.agent.vec_store" in sys.modules:
            del sys.modules["nanobot.agent.vec_store"]
