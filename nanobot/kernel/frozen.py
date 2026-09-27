"""Hashable, read-only mapping for frozen host-contract dataclasses (stdlib only).

``Sampling.logit_bias`` and ``ProviderSpec.extra_headers`` / ``extra_body`` accept
any ``Mapping`` at construction and store a :class:`FrozenMap`, so the frozen
dataclasses that hold them stay hashable. Nested dicts become ``FrozenMap`` and
lists become tuples; :meth:`FrozenMap.as_dict` thaws back to plain JSON-shaped
containers for code that serialises the value (request bodies, pydantic config).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


def freeze(value: Any) -> Any:
    """Recursively convert dicts to ``FrozenMap`` and lists/tuples to tuples."""
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Inverse of :func:`freeze`: ``FrozenMap`` -> dict and tuples -> lists."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


class FrozenMap(Mapping[Any, Any]):
    """Immutable mapping that hashes by content; compares equal to an equal dict."""

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping[Any, Any] | None = None) -> None:
        self._data: dict[Any, Any] = {
            key: freeze(value) for key, value in (data or {}).items()
        }
        self._hash: int | None = None

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(frozenset(self._data.items()))
        return self._hash

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self._data == dict(other.items())
        return NotImplemented

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"

    def as_dict(self) -> dict[Any, Any]:
        """A plain, mutable deep copy (nested maps -> dicts, tuples -> lists)."""
        return thaw(self)


__all__ = ["FrozenMap", "freeze", "thaw"]
