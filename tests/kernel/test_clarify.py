"""Clarification mechanics (Task 24, design section 7).

Headline properties:
- the default classifier is deterministic and LLM-free (I6): equal after
  whitespace/case normalisation is ``minor``, anything else is ``semantic``;
- a minor divergence resolves to a :class:`CommitReady` (no question) that commits
  the known value with the known fact's cite;
- a semantic divergence resolves to exactly one :class:`Question` about one path;
- a divergence with no known source is never committed silently: it is a question;
- the classifier is pluggable, and a bad label fails closed;
- two ambiguities are two independent questions: there is no batching entry point;
- :func:`record_answer` records a ``user`` fact, then commits it with that cite.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import nanobot.kernel.clarify as clarify
from nanobot.kernel.artifacts import ArtifactNotFoundError, ArtifactStore, CitationError
from nanobot.kernel.clarify import (
    CommitReady,
    Divergence,
    Question,
    default_classifier,
    record_answer,
    resolve_divergence,
)
from nanobot.kernel.facts import FactStore


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Server(BaseModel):
    hostname: str
    cores: int = 1
    address: Address | None = None


@pytest.fixture
def facts(tmp_path: Path):
    f = FactStore(tmp_path / "state")
    yield f
    f.close()


@pytest.fixture
def store(tmp_path: Path, facts: FactStore):
    s = ArtifactStore(tmp_path / "state", facts)
    s.register_kind("server", Server)
    yield s
    s.close()


def _div(proposed: Any, known: Any = None, *, known_trace_id: str | None = "fact-x",
         path: str = "hostname") -> Divergence:
    return Divergence(
        kind="server", artifact_id="art-1", path=path, proposed=proposed,
        known=known, known_trace_id=known_trace_id,
    )


# -- default classifier: exact rule set -------------------------------------------


@pytest.mark.parametrize(("known", "proposed"), [
    ("web-01", "web-01"),
    (4, 4),
    (None, None),
    (["a", "b"], ["a", "b"]),
    ({"k": "v"}, {"k": "v"}),
])
def test_default_exact_match_is_minor(known: Any, proposed: Any) -> None:
    assert default_classifier(_div(proposed, known)) == "minor"


@pytest.mark.parametrize(("known", "proposed"), [
    ("web-01", "  web-01 "),
    ("Web-01", "web-01"),
    ("New  York", "new york"),
    ("line one\nline two", "line one line two"),
    ("STRASSE", "straße"),  # casefold, not lower
    (["Alpha ", "b"], ["alpha", "B"]),  # normalisation recurses into lists
    ({"k": " V"}, {"k": "v"}),  # ... and dict values
])
def test_default_whitespace_or_case_only_is_minor(known: Any, proposed: Any) -> None:
    assert default_classifier(_div(proposed, known)) == "minor"


@pytest.mark.parametrize(("known", "proposed"), [
    ("web-01", "web-02"),
    ("webserver", "web server"),  # inner whitespace added, not just collapsed
    (4, 5),
    (4, "4"),  # a type change is not formatting
    (1, True),  # bool is not int
    (4, 4.0),  # int vs float: conservative, ask
    (["a", "b"], ["b", "a"]),  # order matters
    ({"K": "v"}, {"k": "v"}),  # dict keys are not normalised
    ("web-01", None),
])
def test_default_different_value_is_semantic(known: Any, proposed: Any) -> None:
    assert default_classifier(_div(proposed, known)) == "semantic"


def test_default_no_known_source_is_semantic() -> None:
    assert default_classifier(_div("web-01", "web-01", known_trace_id=None)) == "semantic"


def test_default_classifier_is_deterministic() -> None:
    d = _div("Web-01", "web-01")
    assert {default_classifier(d) for _ in range(20)} == {"minor"}


# -- resolve_divergence -------------------------------------------------------------


def test_minor_divergence_commits_silently_with_known_value_and_cite() -> None:
    out = resolve_divergence(_div(" WEB-01", "web-01", known_trace_id="fact-abc"))
    assert isinstance(out, CommitReady)
    assert not isinstance(out, Question)
    assert out.path == "hostname"
    assert out.value == "web-01"  # the cited fact's value, not the reformatted draft
    assert out.cite == "fact-abc"
    assert out.delta == {"hostname": "web-01"}
    assert out.cites == {"hostname": "fact-abc"}


def test_semantic_divergence_asks_exactly_one_question_about_that_field() -> None:
    out = resolve_divergence(_div("web-02", "web-01", path="address.city"))
    assert isinstance(out, Question)
    assert out.path == "address.city"
    assert out.kind == "server"
    assert out.artifact_id == "art-1"
    assert "address.city" in out.prompt
    assert "'web-01'" in out.prompt and "'web-02'" in out.prompt


def test_no_known_source_is_a_question_even_if_classifier_says_minor() -> None:
    calls: list[Divergence] = []

    def always_minor(d: Divergence) -> str:
        calls.append(d)
        return "minor"

    out = resolve_divergence(_div("web-01", known_trace_id=None), classify=always_minor)
    assert isinstance(out, Question)
    assert calls == []  # nothing to compare: the classifier is not consulted
    assert "no source" in out.prompt


def test_custom_classifier_is_used_instead_of_default() -> None:
    seen: list[Divergence] = []

    def lenient(d: Divergence) -> str:
        seen.append(d)
        return "minor"

    d = _div("web-02", "web-01")
    assert isinstance(resolve_divergence(d), Question)  # default: semantic
    out = resolve_divergence(d, classify=lenient)
    assert isinstance(out, CommitReady)
    assert seen == [d]

    def strict(d: Divergence) -> str:
        return "semantic"

    assert isinstance(resolve_divergence(_div("web-01", "web-01"), classify=strict), Question)


@pytest.mark.parametrize("label", ["Minor", "", None, "unsure", 1])
def test_bad_classifier_label_fails_closed(label: Any) -> None:
    with pytest.raises(ValueError, match="classifier"):
        resolve_divergence(_div("a", "a"), classify=lambda d: label)


def test_resolve_takes_one_divergence_not_a_batch() -> None:
    with pytest.raises(TypeError):
        resolve_divergence([_div("a", "b"), _div("c", "d")])  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [
    {"path": ""}, {"path": "a..b"}, {"path": ".a"}, {"path": 3},
    {"artifact_id": ""}, {"artifact_id": None}, {"kind": ""},
    {"known_trace_id": ""},
])
def test_divergence_validates_routing_fields(bad: dict[str, Any]) -> None:
    kw: dict[str, Any] = dict(kind="server", artifact_id="art-1", path="hostname",
                              proposed="x", known="y", known_trace_id="fact-x")
    kw.update(bad)
    with pytest.raises(ValueError):
        Divergence(**kw)


# -- two ambiguities: two sequential questions, never one merged ----------------------


def test_two_ambiguities_produce_two_independent_questions() -> None:
    d1 = _div("web-02", "web-01", path="hostname")
    d2 = _div("Berlin", "Paris", path="address.city")
    q1, q2 = resolve_divergence(d1), resolve_divergence(d2)
    assert isinstance(q1, Question) and isinstance(q2, Question)
    assert q1 != q2
    assert (q1.path, q2.path) == ("hostname", "address.city")
    # neither question mentions the other's ambiguity
    assert "address.city" not in q1.prompt and "Paris" not in q1.prompt
    assert "hostname" not in q2.prompt and "web-0" not in q2.prompt


def test_no_batching_or_merging_entry_point_exists() -> None:
    public = [n for n in clarify.__all__ if inspect.isfunction(getattr(clarify, n))]
    assert sorted(public) == ["default_classifier", "record_answer", "resolve_divergence"]
    for name in public:
        params = inspect.signature(getattr(clarify, name)).parameters
        assert not any("divergences" in p or "questions" in p for p in params)


# -- record_answer: user provenance, then commit ----------------------------------


def test_record_answer_records_user_fact_then_commits_with_its_cite(
    facts: FactStore, store: ArtifactStore,
) -> None:
    q = resolve_divergence(_div("Berlin", "Paris", path="address.city"))
    assert isinstance(q, Question)
    before = facts.count()
    result = record_answer(facts, store, q, "Berlin", turn_ref="session-1:turn-7")
    assert facts.count() == before + 1
    assert result.artifact_id == "art-1"
    assert store.committed("art-1") == {"address": {"city": "Berlin"}}
    tid = store.citations("art-1")["address.city"]
    assert result.committed == {"address.city": tid}
    fact = facts.resolve(tid)
    assert fact is not None
    assert fact.source_kind == "user"
    assert fact.source_ref == "session-1:turn-7"
    assert fact.value == "Berlin"


def test_two_questions_answered_one_at_a_time(facts: FactStore, store: ArtifactStore) -> None:
    q1 = resolve_divergence(_div("web-02", "web-01", path="hostname"))
    q2 = resolve_divergence(_div("Berlin", "Paris", path="address.city"))
    assert isinstance(q1, Question) and isinstance(q2, Question)
    record_answer(facts, store, q1, "web-02", turn_ref="t-1")
    assert store.committed("art-1") == {"hostname": "web-02"}  # q2 still open
    record_answer(facts, store, q2, "Paris", turn_ref="t-2")
    assert store.committed("art-1") == {"hostname": "web-02", "address": {"city": "Paris"}}
    cites = store.citations("art-1")
    assert cites["hostname"] != cites["address.city"]
    assert {facts.resolve(t).source_ref for t in cites.values()} == {"t-1", "t-2"}


def test_commit_ready_proposes_as_committed(facts: FactStore, store: ArtifactStore) -> None:
    tid = facts.record("document", "doc-1", "web-01", span="12-18")
    out = resolve_divergence(_div("WEB-01 ", "web-01", known_trace_id=tid))
    assert isinstance(out, CommitReady)
    store.propose(out.kind, out.delta, out.cites, artifact_id=out.artifact_id)
    assert store.committed("art-1") == {"hostname": "web-01"}
    assert store.citations("art-1") == {"hostname": tid}


def test_record_answer_with_foreign_fact_store_is_rejected(
    tmp_path: Path, store: ArtifactStore,
) -> None:
    q = resolve_divergence(_div("web-02", "web-01"))
    assert isinstance(q, Question)
    with FactStore(tmp_path / "other") as other, pytest.raises(CitationError):
        record_answer(other, store, q, "web-02", turn_ref="t-1")
    with pytest.raises(ArtifactNotFoundError):
        store.committed("art-1")  # nothing was stored


def test_record_answer_rejects_empty_turn_ref(facts: FactStore, store: ArtifactStore) -> None:
    q = resolve_divergence(_div("web-02", "web-01"))
    assert isinstance(q, Question)
    with pytest.raises(ValueError):
        record_answer(facts, store, q, "web-02", turn_ref="")
    assert facts.count() == 0


def test_module_is_llm_free() -> None:
    src = inspect.getsource(clarify)
    for banned in ("nanobot.providers", "nanobot.agent", "llm_usage", "openai", "anthropic"):
        assert banned not in src
