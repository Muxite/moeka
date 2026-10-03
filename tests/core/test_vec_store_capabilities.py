"""VecStore capability ranking with embeddings cached by content sha256 (spec 011 FR-008).

Needs sqlite-vec (skips otherwise); the embedder is a deterministic fake, so no model
download is involved.
"""

from __future__ import annotations

import zlib

import pytest

pytest.importorskip("sqlite_vec")
np = pytest.importorskip("numpy")

from nanobot.core.vec_store import VecStore  # noqa: E402


class WordEmbedder:
    model_name = "fake-words"
    available = True

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed(self, texts):
        self.embedded.extend(texts)
        out = []
        for text in texts:
            vec = np.zeros(384, dtype=np.float32)
            for word in text.lower().replace(":", " ").split():
                vec[zlib.crc32(word.encode()) % 384] += 1.0
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append(vec / norm)
        return out


@pytest.fixture
def store(tmp_path):
    embedder = WordEmbedder()
    s = VecStore(tmp_path / "vec.db", embedder=embedder)
    if not s.available:
        pytest.skip("vec backend unavailable")
    return s, embedder


def test_rank_capabilities_scores_by_cosine_and_caches_by_sha(store):
    s, embedder = store
    items = [("sha-pdf", "pdf-summarize: summarise pdf documents"),
             ("sha-git", "git-analyze: analyse git repositories")]
    scores = s.rank_capabilities("summarise pdf", items)
    assert scores["sha-pdf"] > scores["sha-git"]
    embedded = len(embedder.embedded)
    s.rank_capabilities("analyse git", items)
    # Only the query was embedded again: capability vectors come from the cache.
    assert len(embedder.embedded) == embedded + 1


def test_rank_capabilities_embeds_only_new_shas(store):
    s, embedder = store
    s.rank_capabilities("q", [("a", "alpha thing")])
    before = list(embedder.embedded)
    s.rank_capabilities("q", [("a", "alpha thing"), ("b", "beta thing")])
    assert embedder.embedded[len(before):] == ["beta thing", "q"]


def test_legacy_skill_methods_still_work(store):
    s, _ = store
    s.upsert_skills([("weather", "get the weather forecast")])
    assert s.search_skills("weather forecast", k=1)
