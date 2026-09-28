"""Epistemics on a kernel (Task 11): facts v2 + ``query``, and ``kernel.epistemics``.

The coach flow end to end: the user states facts about ``person:alice``; a pydantic
dossier kind is registered; a proposal commits its cited leaves and keeps an uncited
one provisional; a divergence yields ONE question; the answer commits with ``user``
provenance; the ``fact.recorded`` / ``artifact.proposed`` events reach a
``kernel.trace`` subscriber inside the host's span.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from moeka.epistemics import (
    ArtifactKindMismatchError,
    ArtifactNotFoundError,
    ArtifactResult,
    CitationError,
    CommitReady,
    Divergence,
    Epistemics,
    FactRecord,
    Question,
)
from nanobot.kernel.facts import FACTS_DB_FILENAME, SCHEMA_VERSION, FactStore
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel


def _env(tmp_path: Path) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )


@pytest.fixture
def kernel(tmp_path):
    k = Kernel(_env(tmp_path))
    yield k
    k.close()


# -- facts schema v2 ----------------------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE facts (
    trace_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('document', 'tool', 'user')),
    source_ref TEXT NOT NULL,
    span TEXT,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX facts_source_idx ON facts(source_kind, source_ref);
PRAGMA user_version = 1;
"""


def _indexes(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'facts'"
        )}
    finally:
        conn.close()


def test_v1_database_migrates_to_v2_idempotently(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / FACTS_DB_FILENAME)
    conn.executescript(_V1_SCHEMA)
    conn.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?)",
        [("fact-" + "a" * 32, "user", "person:alice/age", None, "34", "2026-01-01T00:00:00"),
         ("fact-" + "b" * 32, "document", "doc:cv", "0-10", '"x"', "2026-01-02T00:00:00")],
    )
    conn.commit()
    conn.close()
    assert "facts_ref_idx" not in _indexes(state / FACTS_DB_FILENAME)

    with FactStore(state) as store:
        assert store.schema_version() == SCHEMA_VERSION == 2
        [old] = store.query(ref_prefix="person:alice/")
        assert old.trace_id == "fact-" + "a" * 32 and old.value == 34
    assert "facts_ref_idx" in _indexes(state / FACTS_DB_FILENAME)
    with FactStore(state) as store:  # reopening a v2 file is a no-op
        assert store.schema_version() == 2 and store.count() == 2


def test_query_filters_and_orders_by_creation(tmp_path: Path) -> None:
    with FactStore(tmp_path / "state") as store:
        ids = [
            store.record("user", "person:alice/relationship", "sister"),
            store.record("user", "person:alicex/age", 3),
            store.record("tool", "person:alice/age", 34),
            store.record("document", "doc:notes", "n"),
            store.record("user", "person:alice/job", "nurse"),
        ]
        assert [f.trace_id for f in store.query()] == ids
        alice = store.query(ref_prefix="person:alice/")
        assert [f.trace_id for f in alice] == [ids[0], ids[2], ids[4]]
        assert all(isinstance(f, FactRecord) for f in alice)
        assert [f.value for f in store.query(ref_prefix="person:alice/", source_kind="user")] \
            == ["sister", "nurse"]
        assert len(store.query(ref_prefix="person:alice")) == 4  # alicex matches too
        assert [f.trace_id for f in store.query(limit=2)] == ids[:2]
        assert store.query(limit=0) == []
        assert store.query(ref_prefix="nobody:") == []
        assert len(store.query(ref_prefix="")) == 5
        # Prefix scans are exact, never LIKE patterns.
        store.record("user", "a%b_c", 1)
        assert store.query(ref_prefix="a%") and not store.query(ref_prefix="ab")
        store.record("user", "z\U0010ffffq", 2)
        assert len(store.query(ref_prefix="z\U0010ffff")) == 1
        for bad in ({"source_kind": "model"}, {"limit": -1}, {"limit": True},
                    {"ref_prefix": 3}):
            with pytest.raises(ValueError):
                store.query(**bad)


# -- the facade ---------------------------------------------------------------------


class Dossier(BaseModel):
    name: str
    relationship: str
    job: str | None = None
    tone: str | None = None


def test_epistemics_is_lazy_shared_and_closed_with_the_kernel(tmp_path: Path) -> None:
    kernel = Kernel(_env(tmp_path))
    state = kernel.env.paths.state_dir
    assert not (state / FACTS_DB_FILENAME).exists()
    epi = kernel.epistemics
    assert isinstance(epi, Epistemics) and kernel.epistemics is epi
    fid = epi.record_fact("x", source="user", ref="person:bob/name")
    assert epi.fact(fid).source_kind == "user"
    assert epi.fact("fact-nope") is None
    kernel.close()
    assert epi.closed
    with pytest.raises(RuntimeError, match="closed"):
        epi.fact(fid)
    with pytest.raises(RuntimeError, match="closed"):
        kernel.epistemics  # noqa: B018
    # Persisted under state_dir.
    with FactStore(state) as store:
        assert store.resolve(fid).value == "x"


def test_artifact_views_check_the_kind(kernel) -> None:
    epi = kernel.epistemics
    epi.register_kind("dossier", Dossier)

    class Other(BaseModel):
        x: int

    epi.register_kind("other", Other)
    res = epi.propose("dossier", {"name": "Alice"}, cites={})
    assert res.provisional == ("name",) and res.committed == {}
    with pytest.raises(ArtifactKindMismatchError):
        epi.artifact("other", res.artifact_id)
    with pytest.raises(ArtifactNotFoundError):
        epi.artifact("dossier", "art-missing")
    with pytest.raises(CitationError):
        epi.propose("dossier", {"name": "A"}, cites={"name": "fact-forged"})
    with pytest.raises(TypeError):
        epi.propose("dossier", {"name": "A"}, cites=None)  # type: ignore[arg-type]
    assert set(epi.kinds()) == {"dossier", "other"}


def test_coach_flow_end_to_end(kernel) -> None:
    epi = kernel.epistemics
    seen: list[dict[str, Any]] = []
    kernel.trace.subscribe(None, seen.append)

    with kernel.trace.span("coach.turn", session="s1") as span:
        # 1. The user states facts about alice (subject in the ref prefix).
        rel = epi.record_fact("older sister", source="user", ref="person:alice/relationship",
                              span="turn:3")
        name = epi.record_fact("Alice", source="user", ref="person:alice/name")
        epi.record_fact("Bob", source="user", ref="person:bob/name")
        about = epi.facts(ref_prefix="person:alice/")
        assert [f.trace_id for f in about] == [rel, name]
        assert epi.facts(ref_prefix="person:", source="tool") == []

        # 2. A dossier kind; 3. a proposal: cited leaves commit, the uncited stays provisional.
        epi.register_kind("dossier", Dossier)
        res = epi.propose(
            "dossier",
            {"name": "Alice", "relationship": "older sister", "tone": "warm"},
            cites={"name": name, "relationship": rel},
        )
        assert isinstance(res, ArtifactResult)
        aid = res.artifact_id
        assert res.committed == {"name": name, "relationship": rel}
        assert res.provisional == ("tone",)
        assert epi.artifact("dossier", aid) == {"name": "Alice", "relationship": "older sister"}
        assert epi.provisional("dossier", aid) == {"tone": "warm"}
        assert epi.citations("dossier", aid) == {"name": name, "relationship": rel}

        # 4. A persona needs an unsupported assumption (her job) -> ONE question.
        q = epi.reconcile(Divergence(kind="dossier", artifact_id=aid, path="job",
                                     proposed="teacher"))
        assert isinstance(q, Question) and q.path == "job"
        # A formatting-only difference against a known fact commits silently.
        ready = epi.reconcile(Divergence(kind="dossier", artifact_id=aid, path="name",
                                         proposed="  alice ", known="Alice", known_trace_id=name))
        assert isinstance(ready, CommitReady) and ready.value == "Alice"
        epi.propose("dossier", ready.delta, cites=ready.cites, artifact_id=aid)

        # 5. The answer commits with user provenance.
        done = epi.answer(q, "nurse", turn_ref="s1:turn:4")
        job_fact = done.committed["job"]
        record = epi.fact(job_fact)
        assert record.source_kind == "user" and record.source_ref == "s1:turn:4"
        assert record.value == "nurse"
        model = epi.artifact_model("dossier", aid)
        assert model == Dossier(name="Alice", relationship="older sister", job="nurse")

    recorded = [e for e in seen if e["event"] == "fact.recorded"]
    proposed = [e for e in seen if e["event"] == "artifact.proposed"]
    assert len(recorded) == 4 and len(proposed) == 3
    assert recorded[0]["fact_id"] == rel and recorded[0]["source_span"] == "turn:3"
    assert recorded[-1]["fact_id"] == job_fact
    assert proposed[0]["committed"] == {"name": name, "relationship": rel}
    assert proposed[0]["provisional"] == ["tone"]
    for event in recorded + proposed:
        assert event["trace_id"] == span.trace_id
        assert event["span"] == span.path
        assert event["tags"]["session"] == "s1"
        assert "value" not in event  # values never go on the trace
