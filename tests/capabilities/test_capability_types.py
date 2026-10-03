"""The type vocabulary (spec 011 FR-001)."""

from __future__ import annotations

import pytest

from nanobot.capabilities.types import (
    UnknownTypeError,
    ancestors,
    closure,
    is_type,
    load_vocabulary,
    satisfies,
)

REQUIRED = {
    "any": None, "text": "any", "text/query": "text", "text/markdown": "text",
    "text/code": "text", "text/code:python": "text/code", "text/code:shell": "text/code",
    "text/log": "text", "data": "any", "data/json": "data", "data/csv": "data",
    "data/yaml": "data", "file": "any", "file:pdf": "file", "file:image": "file",
    "file:audio": "file", "file:video": "file", "file:archive": "file", "file:office": "file",
    "url": "any", "url/web": "url", "url/git": "url", "url/video": "url", "path": "any",
    "path/dir": "path", "repo:git": "path/dir", "workspace": "any", "host": "any", "none": None,
}


def test_vocabulary_has_every_required_type_with_its_parent():
    vocab = load_vocabulary()
    for name, parent in REQUIRED.items():
        assert vocab[name] == parent, name


def test_vocabulary_is_read_only():
    with pytest.raises(TypeError):
        load_vocabulary()["new"] = "any"  # type: ignore[index]


def test_is_type():
    assert is_type("file:pdf") and is_type("none") and is_type("any")
    assert not is_type("file:docx") and not is_type("") and not is_type(None)
    assert not is_type("FILE:PDF")


def test_ancestors_self_first_any_last():
    assert ancestors("repo:git") == ("repo:git", "path/dir", "path", "any")
    assert ancestors("text/code:python") == ("text/code:python", "text/code", "text", "any")
    assert ancestors("any") == ("any",)
    assert ancestors("none") == ("none",)
    for name in load_vocabulary():
        chain = ancestors(name)
        assert chain[0] == name
        assert chain[-1] == ("none" if name == "none" else "any")


def test_satisfies_is_the_ancestor_relation():
    assert satisfies("file:pdf", "file")
    assert satisfies("file:pdf", "any")
    assert satisfies("file:pdf", "file:pdf")
    assert not satisfies("file", "file:pdf")
    assert not satisfies("url/git", "path")
    assert satisfies("repo:git", "path")
    assert not satisfies("none", "any")


@pytest.mark.parametrize("call", [
    lambda: ancestors("file:docx"),
    lambda: satisfies("file:docx", "file"),
    lambda: satisfies("file:pdf", "file:docx"),
])
def test_unknown_types_raise(call):
    with pytest.raises(UnknownTypeError):
        call()


def test_unknown_type_error_is_a_value_error():
    assert issubclass(UnknownTypeError, ValueError)


def test_closure_pairs_match_ancestors():
    pairs = set(closure())
    for name in load_vocabulary():
        assert {anc for t, anc in pairs if t == name} == set(ancestors(name))
