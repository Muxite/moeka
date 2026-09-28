"""Epistemics: ``kernel.epistemics`` records facts with provenance, builds typed
artifacts whose committed leaves cite them, and turns an unsupported or conflicting
draft value into one clarifying :class:`Question` (or a :class:`CommitReady`).

A fact's subject is carried by its ``ref`` prefix (``person:alice/relationship``).
"""

from nanobot.kernel.artifacts import (
    ArtifactError,
    ArtifactIncompleteError,
    ArtifactKindMismatchError,
    ArtifactNotFoundError,
    ArtifactResult,
    ArtifactValidationError,
    CitationError,
    UnknownKindError,
)
from nanobot.kernel.clarify import CommitReady, Divergence, Question
from nanobot.kernel.epistemics import Epistemics
from nanobot.kernel.facts import FactRecord

__all__ = [
    "ArtifactError",
    "ArtifactIncompleteError",
    "ArtifactKindMismatchError",
    "ArtifactNotFoundError",
    "ArtifactResult",
    "ArtifactValidationError",
    "CitationError",
    "CommitReady",
    "Divergence",
    "Epistemics",
    "FactRecord",
    "Question",
    "UnknownKindError",
]
