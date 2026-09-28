"""Epistemics (Task 11): ``kernel.epistemics``, facts with provenance and cited artifacts.

One facade over the three P5 pieces, built from ``kernel.core_env`` so every
``fact.recorded`` / ``artifact.proposed`` / ``artifact.rejected`` event reaches
``kernel.trace`` subscribers (inside whatever span is open):

- **Facts** (:class:`~nanobot.kernel.facts.FactStore`, ``<state_dir>/facts.db``):
  :meth:`Epistemics.record_fact` stores a value with its source (``"user"``, ``"tool"``
  or ``"document"``) and a ``ref`` naming it; :meth:`Epistemics.fact` resolves an id;
  :meth:`Epistemics.facts` lists them in creation order.
- **Artifacts** (:class:`~nanobot.kernel.artifacts.ArtifactStore`, ``artifacts.db``,
  resolving cites against the same fact store): :meth:`Epistemics.register_kind` a
  pydantic model, then :meth:`Epistemics.propose` deltas. A leaf cited with a fact id
  commits; an uncited leaf stays provisional; a cite to no fact rejects the whole
  proposal. :meth:`Epistemics.artifact` is the committed view.
- **Clarify** (:mod:`nanobot.kernel.clarify`): :meth:`Epistemics.reconcile` turns one
  :class:`~nanobot.kernel.clarify.Divergence` into a ``CommitReady`` (minor formatting;
  commit the known value) or ONE ``Question``; :meth:`Epistemics.answer` records the
  user's answer as a ``user`` fact and commits it citing that fact.

Subject convention: a fact's subject lives in its ``ref``, as a path-like prefix, e.g.
``person:alice/relationship`` or ``person:alice/turn:17``. ``facts(ref_prefix=
"person:alice/")`` is then everything known about alice. No subject column exists.

Concurrency: the stores are synchronous SQLite behind per-store locks, safe from any
thread. There are no ``async`` variants: an asyncio host calls them through
``asyncio.to_thread`` when the call may block (they are single-row writes and indexed
reads, typically well under a millisecond).

Lifetime: built on first access to ``kernel.epistemics``; closed by
``kernel.close()`` / ``aclose()``, after which every method raises ``RuntimeError``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from nanobot.kernel.artifacts import ArtifactKindMismatchError, ArtifactResult, ArtifactStore
from nanobot.kernel.clarify import (
    ClassifierFn,
    CommitReady,
    Divergence,
    Question,
    default_classifier,
    record_answer,
    resolve_divergence,
)
from nanobot.kernel.facts import FactRecord, FactStore

if TYPE_CHECKING:
    from pydantic import BaseModel

    from nanobot.kernel.env import CoreEnvironment

FactSource = Literal["user", "tool", "document"]


class Epistemics:
    """Facts, cited artifacts and clarification over one ``state_dir``. See the module
    docstring; build through ``kernel.epistemics``."""

    def __init__(self, env: CoreEnvironment) -> None:
        self._facts = FactStore.from_env(env)
        try:
            self._artifacts = ArtifactStore.from_env(env, facts=self._facts)
        except BaseException:
            self._facts.close()
            raise
        self._closed = False
        self._lock = threading.Lock()

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError("kernel is closed: epistemics cannot be used")

    @property
    def fact_store(self) -> FactStore:
        """The underlying :class:`FactStore` (for advanced use)."""
        self._check()
        return self._facts

    @property
    def artifact_store(self) -> ArtifactStore:
        """The underlying :class:`ArtifactStore` (for advanced use)."""
        self._check()
        return self._artifacts

    # -- facts -----------------------------------------------------------------

    def record_fact(
        self, value: Any, *, source: FactSource, ref: str, span: str | None = None,
    ) -> str:
        """Record *value* with its provenance; return the new fact id (``fact-...``).

        ``source`` is where the value came from (``user`` turn, ``tool`` output or
        ``document``); ``ref`` names it and carries the subject
        (``person:alice/relationship``); ``span`` optionally locates it in the source.
        """
        self._check()
        return self._facts.record(source, ref, value, span=span)

    def fact(self, fact_id: str) -> FactRecord | None:
        """The fact behind *fact_id*, or ``None``."""
        self._check()
        return self._facts.resolve(fact_id)

    def facts(
        self,
        *,
        ref_prefix: str | None = None,
        source: FactSource | None = None,
        limit: int | None = None,
    ) -> list[FactRecord]:
        """Facts in creation order, filtered by ``ref`` prefix and/or source."""
        self._check()
        return self._facts.query(ref_prefix=ref_prefix, source_kind=source, limit=limit)

    # -- artifacts --------------------------------------------------------------

    def register_kind(
        self, name: str, model: type[BaseModel], *, replace: bool = False,
    ) -> None:
        """Register the pydantic model for artifact kind *name* (idempotent)."""
        self._check()
        self._artifacts.register_kind(name, model, replace=replace)

    def kinds(self) -> dict[str, type[BaseModel]]:
        self._check()
        return self._artifacts.kinds()

    def propose(
        self,
        kind: str,
        delta: Mapping[str, Any],
        *,
        cites: Mapping[str, str],
        artifact_id: str | None = None,
    ) -> ArtifactResult:
        """Merge *delta* into an artifact (new one when ``artifact_id`` is ``None``).

        ``cites`` maps dotted leaf paths to fact ids: those leaves commit, the others
        stay provisional. Raises an ``ArtifactError`` subclass and stores nothing when
        the delta does not fit the kind or a cite resolves to no fact.
        """
        self._check()
        if not isinstance(delta, Mapping):
            from nanobot.kernel.artifacts import ArtifactValidationError

            raise ArtifactValidationError("delta must be a mapping")
        if not isinstance(cites, Mapping):
            raise TypeError("cites must be a mapping of leaf path -> fact id")
        return self._artifacts.propose(
            kind, dict(delta), dict(cites), artifact_id=artifact_id,
        )

    def _of_kind(self, kind: str, artifact_id: str) -> None:
        actual = self._artifacts.kind_of(artifact_id)
        if actual != kind:
            raise ArtifactKindMismatchError(
                f"artifact {artifact_id!r} is of kind {actual!r}, not {kind!r}"
            )

    def artifact(self, kind: str, artifact_id: str) -> dict[str, Any]:
        """The committed leaves as a (possibly partial) nested dict.

        Raises ``ArtifactNotFoundError`` for an unknown id and
        ``ArtifactKindMismatchError`` when it is of another kind.
        """
        self._check()
        self._of_kind(kind, artifact_id)
        return self._artifacts.committed(artifact_id)

    def artifact_model(self, kind: str, artifact_id: str) -> BaseModel:
        """The committed leaves validated as the kind's model (``ArtifactIncompleteError``
        while required fields are missing)."""
        self._check()
        self._of_kind(kind, artifact_id)
        return self._artifacts.committed_model(artifact_id)

    def provisional(self, kind: str, artifact_id: str) -> dict[str, Any]:
        """The uncited (provisional) leaves, never part of :meth:`artifact`."""
        self._check()
        self._of_kind(kind, artifact_id)
        return self._artifacts.provisional(artifact_id)

    def citations(self, kind: str, artifact_id: str) -> dict[str, str]:
        """Committed leaf path -> the fact id it cites."""
        self._check()
        self._of_kind(kind, artifact_id)
        return self._artifacts.citations(artifact_id)

    # -- clarify ---------------------------------------------------------------

    def reconcile(
        self, divergence: Divergence, *, classify: ClassifierFn = default_classifier,
    ) -> Question | CommitReady:
        """Decide one divergence: ``CommitReady`` (minor) or one ``Question``. Pure:
        nothing is written (commit a ``CommitReady`` with :meth:`propose`)."""
        self._check()
        return resolve_divergence(divergence, classify=classify)

    def answer(self, question: Question, answer: Any, *, turn_ref: str) -> ArtifactResult:
        """Record *answer* as a ``user`` fact (ref ``turn_ref``), then commit it to the
        question's leaf citing that fact."""
        self._check()
        return record_answer(self._facts, self._artifacts, question, answer, turn_ref)

    # -- lifecycle ---------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close both stores. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._artifacts.close()
        finally:
            self._facts.close()


__all__ = ["Epistemics", "FactSource"]
