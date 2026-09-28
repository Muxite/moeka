"""Clarification mechanics: minor vs semantic divergence (Task 24, design section 7).

Flow (design section 7, "Epistemic audit" -> "Commit" / "Generate targeted question"):
- A caller that holds a draft value for one artifact leaf builds a :class:`Divergence`:
  the leaf's routing (``kind``, ``artifact_id``, dotted ``path``), the ``proposed``
  value and, when one exists, the ``known`` value with the ``known_trace_id`` of the
  fact that grounds it (a document span, a tool output or an earlier user turn).
- :func:`resolve_divergence` is a PURE decision function. It returns either:
  - :class:`CommitReady`: the value and cite to pass to ``ArtifactStore.propose``
    (the "minor formatting commits without a question" branch), or
  - :class:`Question`: exactly one targeted question about this one leaf.
  It never writes a store and never talks to a user; the caller does both.
- :func:`record_answer` is the "user answered" branch: it records the answer as a
  ``user`` fact (``FactStore.record("user", turn_ref, answer)``) and then commits it
  with ``ArtifactStore.propose`` citing that new trace ID.

```mermaid
flowchart TD
    D["Divergence(path, known, known_trace_id, proposed)"] --> K{"known source?<br/>(known_trace_id set)"}
    K -- "No" --> Q["Question (single ambiguity)"]
    K -- "Yes" --> C{"classify(divergence)"}
    C -- "minor" --> R["CommitReady(value=known, cite=known_trace_id)"]
    C -- "semantic" --> Q
    C -- "other label" --> E["ValueError (fail closed)"]
    Q --> A["record_answer: FactStore.record('user', turn_ref, answer)"]
    A --> P["ArtifactStore.propose(delta, cites={path: new trace ID})"]
```

Default classifier (:func:`default_classifier`), deterministic and LLM-free (I6):
- No known source (``known_trace_id is None``): ``semantic``. There is nothing to
  compare the draft against, so it is unsupported by provenance.
- Otherwise both values are normalised and compared as canonical JSON
  (``sort_keys=True``), so ``1`` vs ``True`` and ``4`` vs ``4.0`` differ:
  - a string is normalised by collapsing every whitespace run to one space,
    stripping the ends and ``casefold()``-ing it (``"  Web-01 "`` == ``"web-01"``,
    ``"STRASSE"`` == ``"straße"``);
  - list/tuple items and dict VALUES are normalised recursively; dict keys, list
    order and every non-string scalar are compared exactly.
  - equal after normalisation: ``minor``; anything else: ``semantic``.
- Bias: when in doubt, ask. A type change (``4`` vs ``"4"``), added inner whitespace
  (``"webserver"`` vs ``"web server"``) or reordering is semantic.
- Caveat: case is treated as formatting. That is wrong for case-sensitive values
  (POSIX paths, passwords). A minor result therefore commits the KNOWN value, the one
  the cited fact holds, never the reformatted draft: the committed value always
  equals its fact, so a case-folded draft can never overwrite the source's spelling.

Pluggability:
- :data:`ClassifierFn` is the seam: ``Callable[[Divergence], "minor" | "semantic"]``.
  A host or plugin passes a smarter one via ``resolve_divergence(..., classify=fn)``.
- The classifier is consulted only when a known source exists: with nothing to
  compare, the value cannot be committed with a cite, so a question is asked
  regardless (a custom classifier cannot commit an ungrounded value, I3).
- A label other than exactly ``"minor"`` or ``"semantic"`` raises ``ValueError``
  (fail closed; never silently treated as either). Classifier exceptions propagate.

One question per ambiguity (sequencing):
- :func:`resolve_divergence` takes ONE :class:`Divergence` (a list raises
  ``TypeError``) and returns at most one :class:`Question` about that one leaf.
- There is deliberately no batch or merge entry point. A caller with two ambiguities
  calls it twice and gets two independent questions; asking them one at a time in a
  live conversation (and not merging them into one prompt) is the caller's job. The
  turn loop that does so is not built here (design section 7: "the question loop is
  not built", staged after Task 24).

Answers (:func:`record_answer`):
- Records the answer first, then commits (design: "recorded with user provenance
  before the commit"). ``turn_ref`` names the user turn (e.g. ``"<session>:<turn>"``).
- ``fact_store`` must be the store ``artifact_store`` resolves cites against;
  otherwise ``propose`` raises ``CitationError`` and stores nothing.
- If the answer does not fit the kind's model, ``propose`` raises
  ``ArtifactValidationError`` and stores nothing; the ``user`` fact stays recorded,
  since the user did say it (facts are immutable).

Wiring (what is live): ``MoekaKernel.answer(question, answer, turn_ref)`` (Task 25) is a
thin passthrough to :func:`record_answer` over the kernel's own stores. The host calls
:func:`resolve_divergence` (``Kernel.epistemics.reconcile`` / ``.answer`` on the public
kernel, Task 11) and asks the question; no gateway, ``AgentLoop`` or tool
path runs this loop on its own.

Imports: stdlib and kernel modules only; no model or provider imports.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from nanobot.kernel.artifacts import ArtifactResult, ArtifactStore
from nanobot.kernel.facts import FactStore

__all__ = [
    "ClassifierFn",
    "CommitReady",
    "Divergence",
    "Label",
    "Question",
    "default_classifier",
    "record_answer",
    "resolve_divergence",
]

Label = Literal["minor", "semantic"]
_LABELS: frozenset[str] = frozenset({"minor", "semantic"})


def _check_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _check_path(path: Any) -> None:
    _check_str("path", path)
    if any(not seg for seg in path.split(".")):
        raise ValueError(f"path must be a dotted leaf path without empty segments: {path!r}")


def _nest(path: str, value: Any) -> dict[str, Any]:
    out: Any = value
    for seg in reversed(path.split(".")):
        out = {seg: out}
    return out


@dataclass(frozen=True)
class Divergence:
    """A draft value for one artifact leaf, and what is already known about it.

    ``known`` counts only when ``known_trace_id`` is set: a known value is one some
    fact grounds (a JSON ``null`` fact is still a known value).
    """

    kind: str
    artifact_id: str
    path: str
    proposed: Any
    known: Any = None
    known_trace_id: str | None = None

    def __post_init__(self) -> None:
        _check_str("kind", self.kind)
        _check_str("artifact_id", self.artifact_id)
        _check_path(self.path)
        if self.known_trace_id is not None:
            _check_str("known_trace_id", self.known_trace_id)

    @property
    def has_known(self) -> bool:
        return self.known_trace_id is not None


ClassifierFn = Callable[[Divergence], Label]


@dataclass(frozen=True)
class Question:
    """One targeted question about a single ambiguity (one leaf), routable back to it."""

    kind: str
    artifact_id: str
    path: str
    prompt: str
    proposed: Any
    known: Any = None
    known_trace_id: str | None = None


@dataclass(frozen=True)
class CommitReady:
    """A grounded value the caller may commit now: ``propose(kind, delta, cites, ...)``."""

    kind: str
    artifact_id: str
    path: str
    value: Any
    cite: str

    @property
    def delta(self) -> dict[str, Any]:
        return _nest(self.path, self.value)

    @property
    def cites(self) -> dict[str, str]:
        return {self.path: self.cite}


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_normalise(value), sort_keys=True, ensure_ascii=False, default=repr)


def default_classifier(divergence: Divergence) -> Label:
    """Deterministic, LLM-free rule set; see the module docstring for the exact rules."""
    if not divergence.has_known:
        return "semantic"
    if _canonical(divergence.known) == _canonical(divergence.proposed):
        return "minor"
    return "semantic"


def _question(divergence: Divergence) -> Question:
    d = divergence
    if d.has_known:
        prompt = (
            f"For {d.path}, the source says {d.known!r} but the draft has {d.proposed!r}. "
            "Which is correct?"
        )
    else:
        prompt = f"For {d.path}, the draft has {d.proposed!r} but no source supports it. Is it correct?"
    return Question(
        kind=d.kind, artifact_id=d.artifact_id, path=d.path, prompt=prompt,
        proposed=d.proposed, known=d.known, known_trace_id=d.known_trace_id,
    )


def resolve_divergence(
    divergence: Divergence, *, classify: ClassifierFn = default_classifier,
) -> Question | CommitReady:
    """Decide one divergence: commit silently (minor) or ask one question (semantic)."""
    if not isinstance(divergence, Divergence):
        raise TypeError(
            f"resolve_divergence takes one Divergence, got {type(divergence).__name__}; "
            "call it once per ambiguity"
        )
    if not divergence.has_known:
        return _question(divergence)
    label = classify(divergence)
    if not isinstance(label, str) or label not in _LABELS:
        raise ValueError(f"classifier returned {label!r}; expected 'minor' or 'semantic'")
    if label == "semantic":
        return _question(divergence)
    assert divergence.known_trace_id is not None
    return CommitReady(
        kind=divergence.kind, artifact_id=divergence.artifact_id, path=divergence.path,
        value=divergence.known, cite=divergence.known_trace_id,
    )


def record_answer(
    fact_store: FactStore,
    artifact_store: ArtifactStore,
    question: Question,
    answer: Any,
    turn_ref: str,
) -> ArtifactResult:
    """Record ``answer`` as a ``user`` fact, then commit it to the question's leaf."""
    if not isinstance(question, Question):
        raise TypeError(f"record_answer takes a Question, got {type(question).__name__}")
    trace_id = fact_store.record("user", turn_ref, answer)
    return artifact_store.propose(
        question.kind,
        _nest(question.path, answer),
        {question.path: trace_id},
        artifact_id=question.artifact_id,
    )
