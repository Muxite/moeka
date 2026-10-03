"""Part A: the closed type vocabulary (FR-001)."""

from __future__ import annotations

import itertools

import pytest
from _h011 import PARENT, TYPES, cap, ref_ancestors, ref_satisfies, repo_root


@pytest.mark.fr("FR-001")
def test_types_yaml_is_shipped_and_loads():
    assert (repo_root() / "nanobot" / "capabilities" / "types.yaml").is_file()
    vocab = cap("types").load_vocabulary()
    assert vocab is not None


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("t", TYPES)
def test_every_listed_type_is_a_type(t):
    assert cap("types").is_type(t) is True


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("s", ["", "bogus", "file:pdfx", "text/", "File:pdf", "url/ftp",
                               "text/code:rust", " any", "any "])
def test_unknown_strings_are_not_types(s):
    assert cap("types").is_type(s) is False


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("t", TYPES)
def test_ancestors_chain_matches_listed_parents(t):
    got = cap("types").ancestors(t)
    assert isinstance(got, tuple)
    assert got == ref_ancestors(t)


@pytest.mark.fr("FR-001")
def test_ancestors_examples_self_first_any_last():
    t = cap("types")
    assert t.ancestors("any") == ("any",)
    assert t.ancestors("none") == ("none",)
    assert t.ancestors("repo:git") == ("repo:git", "path/dir", "path", "any")
    assert t.ancestors("text/code:python") == ("text/code:python", "text/code", "text", "any")
    assert t.ancestors("file:pdf") == ("file:pdf", "file", "any")
    for name in TYPES:
        chain = t.ancestors(name)
        assert chain[0] == name
        if name != "none":
            assert chain[-1] == "any"


@pytest.mark.fr("FR-001")
def test_satisfies_full_truth_table_over_listed_types():
    t = cap("types")
    for a, b in itertools.product(TYPES, TYPES):
        assert t.satisfies(a, b) is ref_satisfies(a, b), (a, b)


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("task, required, expected", [
    ("file:pdf", "file", True),
    ("file", "file:pdf", False),
    ("file:pdf", "file:pdf", True),
    ("file:pdf", "any", True),
    ("repo:git", "path", True),
    ("path", "repo:git", False),
    ("url/git", "url/web", False),
    ("text/code:shell", "text", True),
    ("none", "any", False),
    ("any", "none", False),
    ("none", "none", True),
    ("any", "any", True),
    ("any", "text", False),
    ("workspace", "host", False),
])
def test_satisfies_examples(task, required, expected):
    assert cap("types").satisfies(task, required) is expected


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("call", [
    lambda t: t.ancestors("bogus"),
    lambda t: t.ancestors(""),
    lambda t: t.satisfies("bogus", "any"),
    lambda t: t.satisfies("file:pdf", "bogus"),
    lambda t: t.satisfies("bogus", "bogus"),
])
def test_unknown_type_raises_unknown_type_error(call):
    t = cap("types")
    assert issubclass(t.UnknownTypeError, Exception)
    with pytest.raises(t.UnknownTypeError):
        call(t)


@pytest.mark.fr("FR-001")
def test_none_has_no_parent_and_nothing_below_it():
    t = cap("types")
    assert PARENT["none"] is None
    for name in TYPES:
        if name != "none":
            assert not t.satisfies(name, "none")
            assert not t.satisfies("none", name)
