"""The closed type vocabulary for capability manifests (spec 011 FR-001).

``types.yaml`` (next to this module) maps each type to its parent. ``any`` is the root
of every type except ``none``, which has no parent and is output-only. ``a`` is an
ancestor of ``t`` when ``a == t`` or ``a`` is on ``t``'s parent chain;
``satisfies(task_type, required)`` asks exactly that.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

VOCABULARY_FILE = Path(__file__).with_name("types.yaml")

ANY = "any"
NONE = "none"


class UnknownTypeError(ValueError):
    """A string that is not in the type vocabulary."""

    def __init__(self, value: object) -> None:
        super().__init__(f"unknown capability type {value!r}")
        self.value = value


class VocabularyError(ValueError):
    """``types.yaml`` is malformed (missing parent, cycle, bad shape)."""


@lru_cache(maxsize=1)
def load_vocabulary() -> Mapping[str, str | None]:
    """``type -> parent`` (``None`` for ``any`` and ``none``), in file order.

    Validated once: every parent exists and every chain ends at a root without a cycle.
    """
    raw = yaml.safe_load(VOCABULARY_FILE.read_text(encoding="utf-8"))
    table = raw.get("types") if isinstance(raw, dict) else None
    if not isinstance(table, dict) or not table:
        raise VocabularyError(f"{VOCABULARY_FILE.name}: expected a non-empty 'types' mapping")
    vocab: dict[str, str | None] = {}
    for name, parent in table.items():
        if not isinstance(name, str) or not (parent is None or isinstance(parent, str)):
            raise VocabularyError(f"{VOCABULARY_FILE.name}: bad entry {name!r}: {parent!r}")
        vocab[name] = parent
    for name in vocab:
        seen: set[str] = set()
        current: str | None = name
        while current is not None:
            if current in seen:
                raise VocabularyError(f"{VOCABULARY_FILE.name}: cycle through {name!r}")
            if current not in vocab:
                raise VocabularyError(f"{VOCABULARY_FILE.name}: {name!r} has unknown parent")
            seen.add(current)
            current = vocab[current]
    if vocab.get(ANY, "") is not None or vocab.get(NONE, "") is not None:
        raise VocabularyError(f"{VOCABULARY_FILE.name}: 'any' and 'none' must be roots")
    return MappingProxyType(vocab)


def is_type(value: object) -> bool:
    """Whether *value* is a string in the vocabulary."""
    return isinstance(value, str) and value in load_vocabulary()


@lru_cache(maxsize=None)
def ancestors(type_name: str) -> tuple[str, ...]:
    """*type_name* and its parent chain: self first, ``any`` last (``none`` -> ``("none",)``).

    Raises :class:`UnknownTypeError` for a string outside the vocabulary.
    """
    vocab = load_vocabulary()
    if not isinstance(type_name, str) or type_name not in vocab:
        raise UnknownTypeError(type_name)
    chain: list[str] = []
    current: str | None = type_name
    while current is not None:
        chain.append(current)
        current = vocab[current]
    return tuple(chain)


def satisfies(task_type: str, required: str) -> bool:
    """Whether *required* is an ancestor of *task_type* (both must be vocabulary types)."""
    if not is_type(required):
        raise UnknownTypeError(required)
    return required in ancestors(task_type)


def closure() -> list[tuple[str, str]]:
    """Every ``(type, ancestor)`` pair, self pairs included (the index's closure table)."""
    return [(name, anc) for name in load_vocabulary() for anc in ancestors(name)]


__all__ = [
    "ANY",
    "NONE",
    "UnknownTypeError",
    "VocabularyError",
    "ancestors",
    "closure",
    "is_type",
    "load_vocabulary",
    "satisfies",
]
