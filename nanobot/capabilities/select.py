"""Deterministic-first capability selection (spec 011 FR-006 to FR-009).

:func:`select` filters the index by types and effects in SQL (FR-007), ranks the
survivors (FR-008) and applies the fallbacks (FR-009). Ranking never removes a
survivor; it only orders them, so the filter alone decides what can be selected.

Rankers are ``ranker(query, survivors) -> {name: score}``. Each survivor is a
:class:`Candidate`: a ``str`` (the name) that also carries ``kind``, ``description``
and ``content_sha256``. A missing name scores 0.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from nanobot.capabilities.index import CapabilityIndex, IndexedCapability
from nanobot.capabilities.manifest import EFFECTS
from nanobot.capabilities.task_types import TaskTyping
from nanobot.capabilities.types import ancestors

if TYPE_CHECKING:
    from nanobot.core.vec_store import VecStore

QUERY_TYPE = "text/query"
_TOKEN = re.compile(r"[a-z0-9]+")


class Candidate(str):
    """A survivor handed to a ranker: its name, plus ``kind``/``description``/sha."""

    kind: str
    description: str
    content_sha256: str

    def __new__(cls, row: IndexedCapability) -> Candidate:
        candidate = super().__new__(cls, row.name)
        candidate.kind = row.kind
        candidate.description = row.description
        candidate.content_sha256 = row.content_sha256
        return candidate

    @property
    def name(self) -> str:
        return str(self)


Ranker = Callable[[str, Sequence[Candidate]], Mapping[str, float]]


@dataclass(frozen=True)
class Selection:
    """The outcome of one selection.

    ``survivors`` are all capabilities that passed the filter, ranked; ``selected`` the
    first ``k`` of them, or every capability in today's order when ``fallback`` is
    ``"empty"`` (nothing survived) or ``"small"`` (the library has at most ``k``).
    ``mode`` is ``"filtered"`` from :func:`select`; ``"all"`` marks a listing that was
    not filtered at all (``selection="all"``, or a degraded index).
    """

    mode: str
    survivors: tuple[str, ...]
    selected: tuple[str, ...]
    fallback: str | None
    task_types: frozenset[str]
    requested_output: str | None = None
    scores: Mapping[str, float] = field(default_factory=dict, compare=False)


def tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (``-``/``_``/punctuation split words)."""
    return _TOKEN.findall((text or "").lower())


def lexical_ranker(query: str, survivors: Sequence[Candidate]) -> dict[str, float]:
    """The deterministic lexical scorer (FR-008).

    score = sum over query tokens shared with the capability's name and description of
    ``1 / log2(2 + df)``, ``df`` = how many survivors contain the token. Pure: the same
    inputs give the same scores in every process.
    """
    query_tokens = set(tokens(query))
    if not query_tokens:
        return {name: 0.0 for name in survivors}
    bags = {
        name: set(tokens(name)) | set(tokens(getattr(name, "description", "")))
        for name in survivors
    }
    df: Counter[str] = Counter()
    for bag in bags.values():
        df.update(bag & query_tokens)
    return {
        name: math.fsum(1.0 / math.log2(2 + df[token]) for token in sorted(bag & query_tokens))
        for name, bag in bags.items()
    }


def vec_ranker(store: VecStore) -> Ranker:
    """A ranker using *store*'s embeddings (cosine of the query with ``name: description``).

    Embeddings are cached in the store by ``content_sha256``. Falls back to the lexical
    scorer when the store cannot embed.
    """

    def rank(query: str, survivors: Sequence[Candidate]) -> dict[str, float]:
        items = [
            (name.content_sha256, f"{name}: {name.description}") for name in survivors
        ]
        by_sha = store.rank_capabilities(query, items) if store.available else None
        if by_sha is None:
            return lexical_ranker(query, survivors)
        return {name: float(by_sha.get(name.content_sha256, 0.0)) for name in survivors}

    return rank


def default_ranker(vec_store: VecStore | None = None) -> Ranker:
    """``vec_ranker`` when *vec_store* can embed, else the lexical scorer."""
    if vec_store is not None and vec_store.available:
        return vec_ranker(vec_store)
    return lexical_ranker


def task_types_for(typing: TaskTyping, ambient: Iterable[str]) -> frozenset[str]:
    """The task types: ``text/query``, the ambient types and the extracted ones."""
    types = frozenset({QUERY_TYPE, *ambient, *typing.types})
    for type_name in types:
        ancestors(type_name)  # UnknownTypeError for a type outside the vocabulary
    return types


def rank(
    survivors: Sequence[IndexedCapability], query: str, ranker: Ranker | None,
) -> tuple[list[IndexedCapability], dict[str, float]]:
    """Order *survivors* by descending score, ties by ``(kind, name)`` ascending."""
    candidates = [Candidate(row) for row in survivors]
    raw = (ranker or lexical_ranker)(query or "", candidates)
    scores = {row.name: float(raw.get(row.name, 0.0)) for row in survivors}
    ordered = sorted(survivors, key=lambda row: (-scores[row.name], row.kind, row.name))
    return ordered, scores


def select(
    index: CapabilityIndex,
    typing: TaskTyping,
    query: str,
    *,
    ambient: Iterable[str] = ("workspace",),
    allowed_effects: Iterable[str] = EFFECTS,
    k: int = 8,
    ranker: Ranker | None = None,
    kinds: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
) -> Selection:
    """Filter *index* for *typing* in SQL, rank the survivors and pick the first *k*.

    ``ranker=None`` uses the deterministic lexical scorer (pass
    ``default_ranker(vec_store)`` for embeddings). *kinds* restricts the selection to
    ``"skill"`` and/or ``"tool"`` capabilities (``None`` = both); *exclude* names
    capabilities listed by other means (always-on skills). The fallbacks count and list
    only what remains.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    kinds = None if kinds is None else tuple(kinds)
    excluded = frozenset(exclude)
    task_types = task_types_for(typing, ambient)
    filtered = index.filter(task_types, typing.requested_output, allowed_effects, kinds=kinds)
    passed = [row for row in filtered if row.name not in excluded]
    ordered, scores = rank(passed, query, ranker)
    survivors = tuple(row.name for row in ordered)

    everything = [row.name for row in index.in_order(kinds) if row.name not in excluded]
    fallback: str | None = None
    if len(everything) <= k:
        fallback = "small"
    elif not survivors:
        fallback = "empty"
    selected = tuple(everything) if fallback is not None else survivors[:k]
    return Selection(
        mode="filtered",
        survivors=survivors,
        selected=selected,
        fallback=fallback,
        task_types=task_types,
        requested_output=typing.requested_output,
        scores=scores,
    )


__all__ = [
    "QUERY_TYPE",
    "Candidate",
    "Ranker",
    "Selection",
    "default_ranker",
    "lexical_ranker",
    "rank",
    "select",
    "task_types_for",
    "tokens",
    "vec_ranker",
]
