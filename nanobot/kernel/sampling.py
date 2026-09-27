"""Per-request sampling parameters (stdlib only).

Pure data in this layer: provider translation (and dropping parameters a
provider does not support) lives with the providers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Sampling:
    """Sampling knobs for one model call; ``None`` / ``()`` means "provider default"."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    repetition_penalty: float | None = None
    logit_bias: Mapping[int | str, float] | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    max_tokens: int | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        # Accept any iterable of strings (a list is the common mistake) but keep
        # the frozen value a tuple; a bare string would split into characters.
        if isinstance(self.stop, str):
            object.__setattr__(self, "stop", (self.stop,))
        elif not isinstance(self.stop, tuple):
            object.__setattr__(self, "stop", tuple(self.stop))


__all__ = ["Sampling"]
