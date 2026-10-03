"""Capability I/O manifests (spec 011 FR-002, FR-003).

A skill declares its ``io`` block in SKILL.md frontmatter; a tool declares the same
dict as its ``Tool.io`` class attribute::

    io:
      inputs: [file:pdf]            # shorthand for {all: [...]}
      # or inputs: {all: [path], any: [file:pdf, file:office]}
      outputs: [text/markdown]
      effects: [read-workspace]

:func:`parse_io` normalises it to an :class:`IOSpec` (``None`` = untyped) and raises
:class:`ManifestError` naming the bad field. Defaults for a present block: no inputs,
``outputs: [any]``, ``effects: [exec]`` (conservative).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from nanobot.capabilities.types import NONE, is_type

EFFECTS: tuple[str, ...] = (
    "none", "read-workspace", "write-workspace", "network", "exec", "sudo",
)
DEFAULT_OUTPUTS: tuple[str, ...] = ("any",)
DEFAULT_EFFECTS: tuple[str, ...] = ("exec",)
_IO_KEYS = ("inputs", "outputs", "effects")
_INPUT_KEYS = ("all", "any")


class ManifestError(ValueError):
    """An invalid ``io`` block; the message names the offending field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True)
class IOSpec:
    """A normalised ``io`` block: every field a sorted tuple of unique strings."""

    inputs_all: tuple[str, ...] = ()
    inputs_any: tuple[str, ...] = ()
    outputs: tuple[str, ...] = DEFAULT_OUTPUTS
    effects: tuple[str, ...] = DEFAULT_EFFECTS

    def to_dict(self) -> dict[str, Any]:
        """The canonical block (round-trips through :func:`parse_io`)."""
        return {
            "inputs": {"all": list(self.inputs_all), "any": list(self.inputs_any)},
            "outputs": list(self.outputs),
            "effects": list(self.effects),
        }


def _string_list(field: str, value: object) -> list[str]:
    if not isinstance(value, list):
        raise ManifestError(field, f"must be a list, got {type(value).__name__}")
    seen: set[object] = set()
    for item in value:
        if not isinstance(item, str):
            raise ManifestError(field, f"entries must be strings, got {item!r}")
        if item in seen:
            raise ManifestError(field, f"duplicate entry {item!r}")
        seen.add(item)
    return list(value)


def _types(field: str, value: object, *, allow_none: bool) -> tuple[str, ...]:
    items = _string_list(field, value)
    for item in items:
        if not is_type(item):
            raise ManifestError(field, f"unknown type {item!r}")
        if item == NONE and not allow_none:
            raise ManifestError(field, "'none' is output-only and cannot be an input")
    return tuple(sorted(items))


def parse_io(raw: object) -> IOSpec | None:
    """Normalise an ``io`` block; ``None`` (no block) means the capability is untyped."""
    if raw is None:
        return None
    if isinstance(raw, IOSpec):
        return raw
    if not isinstance(raw, Mapping):
        raise ManifestError("io", f"must be a mapping, got {type(raw).__name__}")
    unknown = sorted(str(key) for key in raw if key not in _IO_KEYS)
    if unknown:
        raise ManifestError(f"io.{unknown[0]}", f"unknown field (expected one of {_IO_KEYS})")

    inputs_all: tuple[str, ...] = ()
    inputs_any: tuple[str, ...] = ()
    inputs = raw.get("inputs")
    if isinstance(inputs, Mapping):
        bad = sorted(str(key) for key in inputs if key not in _INPUT_KEYS)
        if bad:
            raise ManifestError(f"io.inputs.{bad[0]}", "unknown field (expected 'all' or 'any')")
        if inputs.get("all") is not None:
            inputs_all = _types("io.inputs.all", inputs["all"], allow_none=False)
        if inputs.get("any") is not None:
            inputs_any = _types("io.inputs.any", inputs["any"], allow_none=False)
    elif inputs is not None:
        inputs_all = _types("io.inputs", inputs, allow_none=False)

    outputs = DEFAULT_OUTPUTS
    if raw.get("outputs") is not None:
        outputs = _types("io.outputs", raw["outputs"], allow_none=True)

    effects = DEFAULT_EFFECTS
    if raw.get("effects") is not None:
        values = _string_list("io.effects", raw["effects"])
        for effect in values:
            if effect not in EFFECTS:
                raise ManifestError("io.effects", f"unknown effect {effect!r} (expected {EFFECTS})")
        effects = tuple(sorted(values))

    return IOSpec(inputs_all=inputs_all, inputs_any=inputs_any, outputs=outputs, effects=effects)


def validate_skill(path: str | Path) -> list[str]:
    """Problems with a ``SKILL.md`` (or its directory); an empty list means valid.

    Checks the Agent Skills identity rules (``nanobot.agent.skills.valid_skill_metadata``:
    frontmatter ``name`` equal to the directory name, hyphen-case, <= 64 chars, and a
    1-1024 char ``description``) and, when present, a parseable ``io`` block.
    """
    from nanobot.agent.skills import _SKILL_NAME, parse_skill_metadata

    skill_file = Path(path)
    if skill_file.is_dir():
        skill_file = skill_file / "SKILL.md"
    name = skill_file.parent.name
    try:
        content = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read {skill_file.name}: {exc}"]
    metadata = parse_skill_metadata(content)
    if metadata is None:
        return ["missing or unparseable YAML frontmatter (expected a '---' mapping block)"]

    problems: list[str] = []
    declared = metadata.get("name")
    if declared != name:
        problems.append(f"frontmatter name {declared!r} does not match directory name {name!r}")
    if len(name) > 64 or _SKILL_NAME.fullmatch(name) is None:
        problems.append(
            f"name {name!r} must be hyphen-case (a-z, 0-9, single hyphens), at most 64 chars"
        )
    description = metadata.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        problems.append("description must be a string of 1-1024 characters")
    if "io" in metadata:
        try:
            parse_io(metadata["io"])
        except ManifestError as exc:
            problems.append(f"invalid io block: {exc}")
    return problems


__all__ = [
    "DEFAULT_EFFECTS",
    "DEFAULT_OUTPUTS",
    "EFFECTS",
    "IOSpec",
    "ManifestError",
    "parse_io",
    "validate_skill",
]
