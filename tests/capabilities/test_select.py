"""Selection: ranking and fallbacks (spec 011 FR-006, FR-008, FR-009)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

import pytest

from nanobot.capabilities.index import CapabilityEntry, CapabilityIndex
from nanobot.capabilities.manifest import EFFECTS, IOSpec
from nanobot.capabilities.select import (
    Selection,
    default_ranker,
    lexical_ranker,
    select,
    vec_ranker,
)
from nanobot.capabilities.task_types import Attachment, TaskTyping, extract


def entry(name, description, io=None, *, kind="skill"):
    return CapabilityEntry(kind, name, description, io, f"sha-{kind}-{name}")


PDF = IOSpec(("file:pdf",), (), ("text/markdown",), ("read-workspace",))
GIT = IOSpec(("url/git",), (), ("text/markdown",), ("network",))


def library(extra: int = 12) -> list[CapabilityEntry]:
    items = [
        entry("pdf-summarize", "Summarise a PDF document into markdown notes.", PDF),
        entry("git-analyze", "Analyse a git repository.", GIT),
        entry("pdf-merge", "Merge PDF files.", IOSpec(("file:pdf",), (), ("file:pdf",), ("write-workspace",))),
    ]
    items += [
        entry(f"filler-{i:02d}", f"Unrelated helper number {i}.", IOSpec((), (), ("text",), ("none",)))
        for i in range(extra)
    ]
    return items


@pytest.fixture
def index():
    idx = CapabilityIndex(":memory:")
    idx.sync(library())
    yield idx
    idx.close()


def test_sc002_pdf_markdown_turn_selects_the_summariser(index):
    typing = extract("summarise this as markdown", [Attachment(name="report.pdf")])
    selection = select(index, typing, "summarise this as markdown", ambient=["workspace"],
                       allowed_effects=EFFECTS, k=8)
    assert isinstance(selection, Selection)
    assert selection.mode == "filtered" and selection.fallback is None
    assert selection.selected[0] == "pdf-summarize"
    assert len(selection.selected) <= 8
    assert "git-analyze" not in selection.survivors
    assert "pdf-merge" not in selection.survivors  # file:pdf is not comparable with markdown
    assert selection.task_types == {"text/query", "workspace", "file:pdf"}
    assert selection.selected == selection.survivors[:8]


def test_ranking_is_descending_score_then_kind_name():
    idx = CapabilityIndex(":memory:")
    idx.sync([entry(n, "same words") for n in ("zeta", "alpha", "mid")]
             + [entry("tool-x", "same words", kind="tool")] + library(10))
    scores = {"zeta": 2.0, "alpha": 1.0, "mid": 1.0, "tool-x": 1.0}
    selection = select(idx, TaskTyping(frozenset()), "q", ambient=[], allowed_effects=EFFECTS,
                       k=4, ranker=lambda query, survivors: scores)
    assert selection.selected == ("zeta", "alpha", "mid", "tool-x")


def test_custom_ranker_gets_candidates_that_are_names_with_details(index):
    seen = []

    def ranker(query, survivors):
        seen.extend(survivors)
        return {name: (1.0 if name.description.startswith("Unrelated helper number 3") else 0.0)
                for name in survivors}

    selection = select(index, TaskTyping(frozenset()), "q", ambient=["workspace"],
                       allowed_effects=EFFECTS, k=3, ranker=ranker)
    assert selection.selected[0] == "filler-03"
    assert all(isinstance(c, str) and c.kind == "skill" and c.content_sha256 for c in seen)


def test_lexical_scores_follow_the_formula(index):
    typing = extract("merge pdf", [Attachment(name="a.pdf")])
    selection = select(index, typing, "merge pdf", ambient=[], allowed_effects=EFFECTS, k=5)
    # Survivors: pdf-merge (no requested output), pdf-summarize, 12 fillers. "pdf" is in
    # two survivors (df=2), "merge" in one (df=1).
    expected = 1 / math.log2(2 + 1) + 1 / math.log2(2 + 2)
    assert selection.scores["pdf-merge"] == pytest.approx(expected)
    assert selection.scores["pdf-summarize"] == pytest.approx(1 / math.log2(4))
    assert selection.selected[:2] == ("pdf-merge", "pdf-summarize")


def test_lexical_ranker_splits_names_on_dashes_and_underscores():
    class C(str):
        description = ""

    scores = lexical_ranker("csv tools", [C("csv-clean"), C("my_tools"), C("other")])
    assert scores["csv-clean"] > 0 and scores["my_tools"] > 0 and scores["other"] == 0


def test_lexical_ranking_is_identical_across_processes(tmp_path):
    code = (
        "import json\n"
        "from nanobot.capabilities.select import lexical_ranker\n"
        "class C(str):\n    pass\n"
        "items=[]\n"
        "for n,d in [('a-x','pdf notes merge'),('b','merge'),('c','notes pdf csv')]:\n"
        "    c=C(n); c.description=d; items.append(c)\n"
        "print(json.dumps(lexical_ranker('merge the pdf notes', items), sort_keys=True))\n"
    )
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       check=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout
        for seed in ("1", "2", "3")
    }
    assert len(outputs) == 1
    assert json.loads(outputs.pop())["a-x"] > 0


def test_fallback_empty_lists_everything_in_todays_order():
    idx = CapabilityIndex(":memory:")
    items = [entry(f"git-{i}", "needs git", GIT) for i in range(10)]
    idx.sync(items)
    selection = select(idx, TaskTyping(frozenset()), "hello", ambient=["workspace"],
                       allowed_effects=EFFECTS, k=3)
    assert selection.fallback == "empty" and selection.mode == "filtered"
    assert selection.survivors == ()
    assert selection.selected == tuple(item.name for item in items)


def test_fallback_small_lists_everything_unfiltered_and_unordered():
    idx = CapabilityIndex(":memory:")
    items = [entry("zz-git", "needs git", GIT), entry("aa-plain", "plain")]
    idx.sync(items)
    selection = select(idx, TaskTyping(frozenset()), "plain", ambient=["workspace"],
                       allowed_effects=EFFECTS, k=8)
    assert selection.fallback == "small" and selection.mode == "filtered"
    assert selection.selected == ("zz-git", "aa-plain")
    assert selection.survivors == ("aa-plain",)


def test_kinds_restricts_selection_and_fallback_counts():
    idx = CapabilityIndex(":memory:")
    idx.sync(library(2) + [entry(f"tool-{i}", "a tool", kind="tool") for i in range(20)])
    selection = select(idx, TaskTyping(frozenset()), "tool", ambient=[], allowed_effects=EFFECTS,
                       k=8, kinds=["skill"])
    assert selection.fallback == "small"  # 5 skills <= 8, the 20 tools do not count
    assert all(not name.startswith("tool-") for name in selection.selected)


def test_ambient_types_are_added_by_the_selector(index):
    selection = select(index, TaskTyping(frozenset({"file:pdf"})), "", ambient=["host"],
                       allowed_effects=EFFECTS, k=3)
    assert selection.task_types == {"text/query", "host", "file:pdf"}


def test_k_must_be_positive(index):
    with pytest.raises(ValueError):
        select(index, TaskTyping(frozenset()), "", ambient=[], allowed_effects=EFFECTS, k=0)


class FakeVecStore:
    available = True

    def __init__(self):
        self.calls = []

    def rank_capabilities(self, query, items):
        self.calls.append(items)
        return {sha: (1.0 if "merge" in text else 0.1) for sha, text in items}


def test_vec_ranker_scores_by_content_sha(index):
    store = FakeVecStore()
    selection = select(index, TaskTyping(frozenset({"file:pdf"})), "combine", ambient=[],
                       allowed_effects=EFFECTS, k=3, ranker=default_ranker(store))
    assert selection.selected[0] == "pdf-merge"
    shas = {sha for sha, _ in store.calls[0]}
    assert "sha-skill-pdf-merge" in shas
    assert ("sha-skill-pdf-merge", "pdf-merge: Merge PDF files.") in store.calls[0]


def test_vec_ranker_falls_back_to_lexical_when_the_store_cannot_rank():
    class Down(FakeVecStore):
        def rank_capabilities(self, query, items):
            return None

    store = Down()

    class C(str):
        description = "merge things"
        content_sha256 = "s"

    assert vec_ranker(store)("merge", [C("x")]) == lexical_ranker("merge", [C("x")])
    store.available = False
    assert default_ranker(store) is lexical_ranker
    assert default_ranker(None) is lexical_ranker
