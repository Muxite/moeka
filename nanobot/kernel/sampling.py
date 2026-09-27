"""Per-request sampling parameters (stdlib only).

Pure data in this layer: provider translation (and dropping parameters a
provider does not support) lives with the providers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

from nanobot.kernel.frozen import FrozenMap


@dataclass(frozen=True)
class Sampling:
    """Sampling knobs for one model call; ``None`` / ``()`` means "provider default".

    Hashable: ``logit_bias`` accepts any mapping and is stored as a
    :class:`~nanobot.kernel.frozen.FrozenMap` (``dict(s.logit_bias)`` for a copy).
    """

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
        if self.logit_bias is not None and not isinstance(self.logit_bias, FrozenMap):
            object.__setattr__(self, "logit_bias", FrozenMap(self.logit_bias))

    def set_fields(self) -> tuple[str, ...]:
        """Names of the fields this call sets (non-``None``, non-empty ``stop``)."""
        return tuple(
            f.name for f in fields(self)
            if getattr(self, f.name) is not None and getattr(self, f.name) != ()
        )


__all__ = ["Sampling"]
