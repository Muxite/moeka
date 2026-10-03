"""Part D: the selector (FR-006 to FR-009, SC-002 at index level, SC-003 soundness)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys

import pytest
from _h011 import (
    EFFECTS,
    INPUT_TYPES,
    TYPES,
    att,
    cap,
    close_index,
    const_ranker,
    entry,
    open_index,
    ref_lexical_order,
    ref_survives,
    run_select,
    table_ranker,
)

PROPERTY_EXAMPLES = 500


@pytest.fixture
def mk(tmp_path):
    """``mk(entries)`` -> a synced index (closed at teardown)."""
    made = []

    def make(entries):
        idx = open_index(tmp_path / f"sel{len(made)}.db")
        made.append(idx)
        idx.sync(list(entries))
        return idx

    yield make
    for idx in made:
        close_index(idx)


def _filler(n, req="url/video"):
    return [entry("skill", f"zz-filler-{i}", "filler", {"inputs": [req]}) for i in range(n)]


# -- FR-006 -------------------------------------------------------------------------------------


@pytest.mark.fr("FR-006")
def test_selection_fields(mk):
    idx = mk([entry("skill", "a", "alpha"), entry("skill", "b", "beta"),
              entry("tool", "c", "gamma")])
    sel = run_select(idx, {"file:pdf"}, "text/markdown", query="alpha", k=2)
    assert isinstance(sel, cap("select").Selection)
    assert sel.mode in ("filtered", "all")
    assert isinstance(sel.survivors, tuple) and isinstance(sel.selected, tuple)
    assert all(isinstance(n, str) for n in sel.survivors + sel.selected)
    assert sel.fallback in (None, "empty", "small")
    assert isinstance(sel.task_types, frozenset)


@pytest.mark.fr("FR-006")
def test_selected_is_first_k_survivors(mk):
    idx = mk([entry("skill", n, n) for n in "abcdefg"])
    sel = run_select(idx, k=3, ranker=table_ranker({"e": 5, "b": 4, "g": 3, "a": 2}))
    assert sel.mode == "filtered" and sel.fallback is None
    assert sel.survivors == ("e", "b", "g", "a", "c", "d", "f")
    assert sel.selected == ("e", "b", "g")


@pytest.mark.fr("FR-006")
def test_task_types_include_extracted_and_ambient(mk):
    idx = mk([entry("skill", n, n) for n in "abcdefghij"])
    sel = run_select(idx, {"file:pdf", "path"}, ambient=("text/query", "workspace", "host"))
    assert {"file:pdf", "path", "text/query", "workspace", "host"} <= sel.task_types


@pytest.mark.fr("FR-006", "FR-007")
def test_text_query_is_always_ambient(mk):
    """"text/query always": the selector adds it even when ``ambient`` omits it."""
    idx = mk([entry("skill", "needs-query", "q", {"inputs": ["text/query"]}), *_filler(9)])
    sel = run_select(idx, ambient=("workspace",), k=2, ranker=const_ranker)
    assert "text/query" in sel.task_types
    assert sel.survivors == ("needs-query",)


# -- FR-007 rules -------------------------------------------------------------------------------


def _survivors(mk, caps, types=(), requested=None, allowed=EFFECTS,
               ambient=("text/query", "workspace")):
    idx = mk([*caps, *_filler(9)])
    sel = run_select(idx, types, requested, allowed=allowed, ambient=ambient, k=1,
                     ranker=const_ranker)
    return set(sel.survivors)


@pytest.mark.fr("FR-007")
def test_rule1_unavailable_tool_dropped_unavailable_skill_kept(mk):
    got = _survivors(mk, [
        entry("tool", "dead-tool", "t", {"effects": ["none"]}, available=False),
        entry("skill", "dead-skill", "s", {"effects": ["none"]}, available=False),
        entry("tool", "live-tool", "t", {"effects": ["none"]}),
        entry("tool", "dead-untyped-tool", "t", None, available=False),
        entry("skill", "dead-untyped-skill", "s", None, available=False),
    ])
    assert got == {"dead-skill", "live-tool", "dead-untyped-skill"}


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("effects, allowed, survives", [
    (["network"], ["none", "exec"], False),
    (["network"], ["network"], True),
    (["read-workspace", "write-workspace"], ["read-workspace"], False),
    (["read-workspace", "write-workspace"], ["read-workspace", "write-workspace"], True),
    (["sudo"], ["sudo"], False),
    (["sudo"], ["exec"], False),
    (["sudo"], ["sudo", "exec"], True),
    (["sudo", "exec"], ["sudo"], False),
    (["exec"], ["none", "read-workspace", "write-workspace", "network", "sudo"], False),
    (["none"], list(EFFECTS), True),
])
def test_rule2_effects_policy(mk, effects, allowed, survives):
    got = _survivors(mk, [entry("tool", "t", "t", {"effects": effects})], allowed=allowed)
    assert ("t" in got) is survives


@pytest.mark.fr("FR-007")
def test_rule2_missing_effects_means_exec(mk):
    typed_no_effects = entry("skill", "s", "s", {"inputs": ["file:pdf"]})
    no_exec = ["none", "read-workspace", "write-workspace", "network"]
    assert "s" not in _survivors(mk, [typed_no_effects], types={"file:pdf"}, allowed=no_exec)
    assert "s" in _survivors(mk, [typed_no_effects], types={"file:pdf"}, allowed=["exec"])


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("inputs, types, survives", [
    (["file:pdf"], {"file:pdf"}, True),
    (["file:pdf", "path"], {"file:pdf"}, False),
    (["file:pdf", "path"], {"file:pdf", "path"}, True),
    (["file"], {"file:pdf"}, True),
    (["file:pdf"], {"file"}, False),
    (["path"], {"repo:git"}, True),
    (["any"], set(), True),
    (["workspace"], set(), True),
    (["host"], set(), False),
    (["url/git"], {"url/web"}, False),
    ([], set(), True),
])
def test_rule3_inputs_all(mk, inputs, types, survives):
    got = _survivors(mk, [entry("skill", "s", "s", {"inputs": inputs, "effects": ["none"]})],
                     types=types)
    assert ("s" in got) is survives


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("io_inputs, types, survives", [
    ({"any": ["file:pdf", "file:office"]}, {"file:office"}, True),
    ({"any": ["file:pdf", "file:office"]}, {"file:image"}, False),
    ({"any": ["file"]}, {"file:image"}, True),
    ({"all": ["path"], "any": ["file:pdf", "file:office"]}, {"path", "file:pdf"}, True),
    ({"all": ["path"], "any": ["file:pdf", "file:office"]}, {"file:pdf"}, False),
    ({"all": ["path"], "any": ["file:pdf", "file:office"]}, {"path"}, False),
    ({"all": ["path"], "any": []}, {"path"}, True),
])
def test_rule3_inputs_any(mk, io_inputs, types, survives):
    got = _survivors(mk, [entry("skill", "s", "s", {"inputs": io_inputs, "effects": ["none"]})],
                     types=types)
    assert ("s" in got) is survives


@pytest.mark.fr("FR-007")
def test_rule3_ambient_types_count(mk):
    cap_ = entry("skill", "s", "s", {"inputs": ["host"], "effects": ["none"]})
    assert "s" not in _survivors(mk, [cap_], ambient=("text/query", "workspace"))
    assert "s" in _survivors(mk, [cap_], ambient=("text/query", "workspace", "host"))


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("outputs, requested, survives", [
    (["text/markdown"], "text/markdown", True),
    (["text"], "text/markdown", True),
    (["text/markdown"], "text", True),
    (["data/csv"], "text/markdown", False),
    (["any"], "text/markdown", True),
    (None, "text/markdown", True),
    (["none"], "text/markdown", False),
    (["none"], None, True),
    (["data/csv"], None, True),
    (["data/csv", "text/markdown"], "text/markdown", True),
    (["file"], "file:pdf", True),
    (["file:image"], "file:pdf", False),
    (["text/code:python"], "text/code", True),
])
def test_rule4_outputs(mk, outputs, requested, survives):
    io = {"effects": ["none"]}
    if outputs is not None:
        io["outputs"] = outputs
    got = _survivors(mk, [entry("skill", "s", "s", io)], requested=requested)
    assert ("s" in got) is survives


@pytest.mark.fr("FR-007")
def test_untyped_always_passes_type_rules(mk):
    got = _survivors(mk, [entry("skill", "u", "untyped skill"), entry("tool", "ut", "untyped")],
                     types={"url/git"}, requested="file:video")
    assert {"u", "ut"} <= got


def _random_case(rng):
    allowed = {e for e in EFFECTS if rng.random() < 0.7} | {"none"}
    caps = []
    for j in range(rng.randint(2, 14)):
        kind = rng.choice(["skill", "tool"])
        available = rng.random() < 0.8
        if "exec" in allowed and rng.random() < 0.2:
            io = None
        else:
            io = {}
            r = rng.random()
            if r < 0.4:
                io["inputs"] = rng.sample(INPUT_TYPES, rng.randint(0, 2))
            elif r < 0.8:
                pool = rng.sample(INPUT_TYPES, 5)
                io["inputs"] = {"all": pool[: rng.randint(0, 2)],
                                "any": pool[2: 2 + rng.randint(0, 3)]}
            if rng.random() < 0.75:
                io["outputs"] = rng.sample(TYPES, rng.randint(1, 2))
            if rng.random() < 0.75:
                io["effects"] = rng.sample(EFFECTS, rng.randint(1, 3))
        caps.append((kind, f"c{j:02d}", available, io))
    task = set(rng.sample([t for t in INPUT_TYPES if t != "any"], rng.randint(0, 4)))
    ambient = ["text/query"] + [t for t in ("workspace", "host") if rng.random() < 0.5]
    split = set(rng.sample(sorted(task), rng.randint(0, len(task)))) if task else set()
    requested = None if rng.random() < 0.3 else rng.choice(TYPES)
    return caps, task - split, ambient + sorted(split), requested, allowed


@pytest.mark.fr("FR-007", "FR-008", "SC-003")
@pytest.mark.timeout(900)
def test_soundness_sql_filter_equals_python_reference(tmp_path):
    for example in range(PROPERTY_EXAMPLES):
        rng = random.Random(7_000 + example)
        caps, extracted, ambient, requested, allowed = _random_case(rng)
        idx = open_index(tmp_path / f"s{example}.db")
        try:
            idx.sync([entry(k, n, f"desc {n}", io, available=a) for k, n, a, io in caps])
            sel = run_select(idx, extracted, requested, ambient=ambient, allowed=allowed, k=1,
                             ranker=const_ranker)
        finally:
            close_index(idx)
        task_types = set(extracted) | set(ambient)
        expected = {n for k, n, a, io in caps
                    if ref_survives(k, a, io, task_types, requested, allowed)}
        ctx = (example, sorted(task_types), requested, sorted(allowed))
        assert set(sel.survivors) == expected, ctx
        assert len(sel.survivors) == len(set(sel.survivors)), ctx
        kind_of = {n: k for k, n, _, _ in caps}
        assert list(sel.survivors) == sorted(sel.survivors, key=lambda n: (kind_of[n], n)), ctx
        assert sel.mode == "filtered", ctx
        if expected:
            assert sel.fallback is None, ctx
            assert sel.selected == sel.survivors[:1], ctx
        else:
            assert sel.fallback == "empty", ctx
            assert set(sel.selected) == {n for _, n, _, _ in caps}, ctx


# -- FR-008 ranking -----------------------------------------------------------------------------


@pytest.mark.fr("FR-008")
def test_custom_ranker_is_honoured_and_called_with_survivors(mk):
    calls = []
    idx = mk([entry("skill", "a", "a"), entry("skill", "b", "b"), entry("skill", "c", "c"),
              entry("skill", "dropped", "d", {"inputs": ["url/git"]})])
    sel = run_select(idx, query="the query", k=2,
                     ranker=table_ranker({"a": 0.1, "b": 0.9, "c": 0.5}, calls))
    assert sel.survivors == ("b", "c", "a") and sel.selected == ("b", "c")
    assert calls and calls[-1][0] == "the query"
    assert set(calls[-1][1]) == {"a", "b", "c"}


@pytest.mark.fr("FR-008")
def test_ties_break_by_kind_then_name(mk):
    idx = mk([entry("tool", "alpha", "x"), entry("skill", "zeta", "x"), entry("skill", "beta", "x"),
              entry("tool", "aardvark", "x"), entry("skill", "top", "x")])
    sel = run_select(idx, k=1, ranker=table_ranker({"top": 2.0}))
    assert sel.survivors == ("top", "beta", "zeta", "aardvark", "alpha")


@pytest.mark.fr("FR-008")
def test_negative_and_fractional_scores_order(mk):
    idx = mk([entry("skill", n, n) for n in ("p", "q", "r", "s")])
    sel = run_select(idx, k=1, ranker=table_ranker({"p": -1.5, "q": 0.25, "r": 0.2500001,
                                                     "s": 0.0}))
    assert sel.survivors == ("r", "q", "s", "p")


LEX_LIB = [
    ("skill", "pdf-to-markdown", "Convert PDF files to markdown"),
    ("skill", "pdf-merge", "Merge several PDF files"),
    ("skill", "csv-stats", "Column statistics for tables"),
    ("skill", "markdown-lint", "Lint markdown documents"),
    ("tool", "web_fetch", "Fetch a web page"),
    ("tool", "read_file", "Read a file from the workspace"),
]


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("query", [
    "convert the pdf to markdown",
    "merge these pdf files",
    "lint my markdown",
    "fetch the web page",
    "nothing in common zzz",
])
def test_default_lexical_ranker_matches_reference(mk, query):
    idx = mk([entry(k, n, d) for k, n, d in LEX_LIB])
    sel = run_select(idx, query=query, k=2)
    assert list(sel.survivors) == ref_lexical_order(query, LEX_LIB)


@pytest.mark.fr("FR-008")
def test_lexical_df_is_over_survivors_only(mk):
    """``df`` counts survivors: filtered-out capabilities sharing a token do not lower its
    weight (with df over the whole index the order below would be beta, gamma, alpha)."""
    lib = [("skill", "alpha", "quarterly report"), ("skill", "beta", "helper one"),
           ("skill", "gamma", "helper two"), ("skill", "delta", "nothing here")]
    dropped = [entry("skill", f"hidden-{i}", "report report", {"inputs": ["url/git"]})
               for i in range(5)]
    idx = mk([*(entry(k, n, d) for k, n, d in lib), *dropped])
    sel = run_select(idx, query="report helper", k=1)
    assert list(sel.survivors) == ref_lexical_order("report helper", lib)
    assert list(sel.survivors) == ["alpha", "beta", "gamma", "delta"]


_DET = r"""
import json, sys
from pathlib import Path
from loguru import logger
logger.remove()
from nanobot.capabilities.index import CapabilityEntry, CapabilityIndex
from nanobot.capabilities.manifest import parse_io
from nanobot.capabilities.select import select
from nanobot.capabilities.task_types import extract
lib = json.loads(sys.argv[2])
idx = CapabilityIndex(Path(sys.argv[1]))
idx.sync([CapabilityEntry(kind=k, name=n, description=d,
                          io=parse_io(io) if io is not None else None,
                          content_sha256=s, available=True) for k, n, d, io, s in lib])
out = []
for q in ["summarise the pdf report as markdown", "git repo review", "a b c d e",
          "report report pdf", "convert csv table into json $type:data/csv"]:
    t = extract(q)
    sel = select(idx, t, q, ambient=("text/query", "workspace"),
                 allowed_effects=("none", "read-workspace", "write-workspace", "network",
                                  "exec", "sudo"), k=3)
    out.append([list(sel.survivors), list(sel.selected), sel.fallback])
print(json.dumps(out))
"""


@pytest.mark.fr("FR-008", "FR-005")
def test_lexical_ranker_deterministic_across_processes(tmp_path):
    import hashlib

    rng = random.Random(42)
    words = ["pdf", "report", "markdown", "git", "repo", "review", "csv", "table", "json",
             "summarise", "a", "b", "c", "d", "e"]
    lib = []
    for i in range(40):
        desc = " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
        io = None if i % 3 == 0 else {"inputs": [], "outputs": [rng.choice(["any",
                                                                            "text/markdown",
                                                                            "data/json"])],
                                      "effects": ["none"]}
        name = f"cap-{rng.choice(words)}-{i}"
        lib.append([rng.choice(["skill", "tool"]), name, desc, io,
                    hashlib.sha256(f"{name}{desc}{io}".encode()).hexdigest()])
    outs = set()
    for n, seed in enumerate(("0", "1", "999")):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-c", _DET, str(tmp_path / f"det{n}.db"), json.dumps(lib)],
            env=env, capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        outs.add(proc.stdout.strip().splitlines()[-1])
    assert len(outs) == 1


# -- FR-009 fallbacks ---------------------------------------------------------------------------


@pytest.mark.fr("FR-009")
def test_empty_fallback_selects_everything(mk):
    caps = [entry("skill", f"git-{i}", "git", {"inputs": ["url/git"]}) for i in range(10)]
    idx = mk(caps)
    sel = run_select(idx, {"file:pdf"}, k=3)
    assert sel.mode == "filtered" and sel.fallback == "empty"
    assert sel.survivors == ()
    assert sorted(sel.selected) == sorted(e.name for e in caps)


@pytest.mark.fr("FR-009")
def test_small_fallback_no_filtering(mk):
    caps = [entry("skill", "git-1", "git", {"inputs": ["url/git"]}),
            entry("skill", "pdf-1", "pdf", {"inputs": ["file:pdf"]}),
            entry("tool", "net", "net", {"effects": ["network"]})]
    idx = mk(caps)
    sel = run_select(idx, {"file:pdf"}, allowed=["none"], k=3,
                     ranker=table_ranker({"net": 9.0}))
    assert sel.mode == "filtered" and sel.fallback == "small"
    assert sorted(sel.selected) == ["git-1", "net", "pdf-1"]


@pytest.mark.fr("FR-009")
def test_small_fallback_does_not_reorder(mk):
    caps = [entry("skill", n, f"{n} words") for n in ("delta", "alpha", "charlie", "bravo")]
    idx = mk(caps)
    a = run_select(idx, query="alpha words", k=8)
    b = run_select(idx, query="charlie", k=8, ranker=table_ranker({"bravo": 3.0}))
    assert a.fallback == b.fallback == "small"
    assert a.selected == b.selected
    assert sorted(a.selected) == ["alpha", "bravo", "charlie", "delta"]


@pytest.mark.fr("FR-009")
@pytest.mark.parametrize("total, k, fallback", [(8, 8, "small"), (9, 8, None), (1, 1, "small"),
                                                 (2, 1, None)])
def test_small_boundary(mk, total, k, fallback):
    idx = mk([entry("skill", f"s{i:02d}", "s") for i in range(total)])
    sel = run_select(idx, k=k)
    assert sel.fallback == fallback
    assert len(sel.selected) == (total if fallback == "small" else k)


@pytest.mark.fr("FR-009")
def test_no_fallback_when_some_survive(mk):
    idx = mk([entry("skill", "pdf-1", "pdf", {"inputs": ["file:pdf"]}), *_filler(9)])
    sel = run_select(idx, {"file:pdf"}, k=3)
    assert sel.fallback is None and sel.selected == ("pdf-1",)


# -- SC-002 at index level --------------------------------------------------------------------


def _library_120():
    rng = random.Random(120)
    lib = [entry("skill", "pdf-summariser", "Summarise a PDF document as markdown",
                 {"inputs": ["file:pdf"], "outputs": ["text/markdown"],
                  "effects": ["read-workspace"]})]
    for i in range(30):
        lib.append(entry("skill", f"git-tool-{i:02d}", "Summarise a git repo as markdown",
                         {"inputs": ["url/git"], "outputs": ["text/markdown"],
                          "effects": ["network"]}))
    pool = [t for t in INPUT_TYPES if t not in ("any", "url/git", "file:pdf", "file", "text",
                                                "text/query", "workspace")]
    for i in range(59):
        lib.append(entry("skill", f"typed-{i:02d}", f"helper number {i}",
                         {"inputs": [rng.choice(pool)], "outputs": [rng.choice(TYPES)],
                          "effects": [rng.choice(EFFECTS)]}))
    for i in range(20):
        lib.append(entry("tool", f"tool-{i:02d}", f"tool number {i}",
                         {"effects": [rng.choice(["none", "read-workspace", "exec"])]}))
    for i in range(10):
        lib.append(entry("skill", f"untyped-{i:02d}", f"misc helper {i}"))
    assert len(lib) == 120
    return lib


@pytest.mark.fr("SC-002", "FR-005", "FR-007", "FR-009")
def test_sc002_pdf_attachment_markdown(mk):
    lib = _library_120()
    idx = mk(lib)
    text = "summarise this as markdown"
    typing = cap("task_types").extract(text, attachments=(att(mime="application/pdf",
                                                              name="report.pdf"),))
    assert "file:pdf" in typing.types and typing.requested_output == "text/markdown"
    sel = cap("select").select(idx, typing, text, ambient=("text/query", "workspace"),
                               allowed_effects=EFFECTS, k=8)
    assert sel.fallback is None
    assert len(sel.selected) <= 8
    assert "pdf-summariser" in sel.selected
    git = {e.name for e in lib if e.io is not None and "url/git" in e.io.inputs_all}
    assert not git & set(sel.selected)
    assert not git & set(sel.survivors)
