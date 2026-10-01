"""Variants and fingerprints (Task 8): what the model sees, swappable per kernel.

A :class:`Variant` names a set of overrides for the text the model sees, loaded per
agent loop without code edits and without touching process-wide state (class
attributes, module caches): two loops (or kernels) with different variants in one
process never see each other's overrides.

- **Tool descriptions.** For a tool named ``n``: ``tool_descriptions[n]`` wins, then
  ``tool_descriptions_dir/<n>.txt`` (UTF-8, one trailing newline dropped, like the
  built-in files), then the tool's own description (its built-in data file, or its
  Python text). ``ToolLoader`` applies the override to each tool instance it builds
  (``Tool._description_override``); ``AgentLoop`` does the same for ``my``. The
  schema the model gets (``to_schema``) carries it for every tool; ``.description``
  reflects it for data-file tools. Tools registered outside the loader (MCP servers,
  host actions) are not overridden. A variant's dir file is read when the loader
  builds the tool, never per prompt.
- **Templates.** A file in ``templates_dir`` shadows the built-in template of the same
  relative name (``agent/identity.md``, ``agent/_snippets/...``); missing ones fall
  back. Applies to the main system prompt (``ContextBuilder``) and to the sub-agent
  prompt and announce text. Memory/Dream and runner templates stay built-in.
- **Skills.** ``builtin_skills_dir`` replaces the bundled skills directory for the
  loop's ``SkillsLoader`` (and its sub-agents'); ``read_file`` may read it and maps
  ``skills/<name>/...`` onto it.
- **Bootstrap.** ``bootstrap`` entries merge into ``ContextBuilder`` bootstrap
  overrides (``AGENTS.md``/``SOUL.md``/``USER.md`` shadow the files, other names are
  appended). An explicit ``bootstrap_overrides`` given to the loop (``AgentSpec`` /
  profile bootstrap, ``MoekaCore.set_bootstrap``) wins over the variant's entry of
  the same name.

``Variant()`` (name ``"base"``, no overrides) and ``None`` behave identically: the
built-in text, byte for byte.

:func:`fingerprint` digests everything the model sees for a loop (see its
docstring for exactly what is normalised).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.kernel.frozen import FrozenMap, thaw

if TYPE_CHECKING:
    from nanobot.kernel.sampling import Sampling

# A tool name usable as a file stem: no separators, no leading dot.
_FILE_STEM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _as_path(value: Any, field_name: str) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        return Path(value)
    raise TypeError(f"Variant.{field_name} must be a path or None, got {type(value).__name__}")


def _str_map(value: Any, field_name: str) -> FrozenMap:
    if not isinstance(value, Mapping):
        raise TypeError(f"Variant.{field_name} must be a mapping, got {type(value).__name__}")
    for key, text in value.items():
        if not isinstance(key, str) or not isinstance(text, str):
            raise TypeError(f"Variant.{field_name} maps str -> str, got {key!r}: {text!r}")
    return value if isinstance(value, FrozenMap) else FrozenMap(value)


@dataclass(frozen=True)
class Variant:
    """Overrides for what the model sees (see the module docstring). Hashable."""

    name: str = "base"
    tool_descriptions_dir: Path | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=FrozenMap)
    templates_dir: Path | None = None
    builtin_skills_dir: Path | None = None
    bootstrap: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Variant.name must be a non-empty string")
        for name in ("tool_descriptions_dir", "templates_dir", "builtin_skills_dir"):
            object.__setattr__(self, name, _as_path(getattr(self, name), name))
        for name in ("tool_descriptions", "bootstrap"):
            object.__setattr__(self, name, _str_map(getattr(self, name), name))

    @property
    def template_roots(self) -> tuple[Path, ...]:
        """Template search roots searched before the built-in templates."""
        return (self.templates_dir,) if self.templates_dir is not None else ()

    def description_for(self, tool_name: str) -> str | None:
        """The override for *tool_name*, or ``None`` to keep the tool's own text.

        Reads ``tool_descriptions_dir/<tool_name>.txt`` on each call (the loader calls
        this once per tool it builds). An unreadable or non-UTF-8 file raises
        :class:`~nanobot.agent.tools.base.DescriptionFileError`.
        """
        text = self.tool_descriptions.get(tool_name)
        if text is not None:
            return text
        root = self.tool_descriptions_dir
        if root is None or not _FILE_STEM.fullmatch(tool_name) or ".." in tool_name:
            return None
        path = root / f"{tool_name}.txt"
        if not path.is_file():
            return None
        from nanobot.agent.tools.base import read_description_file  # lazy: import cycle

        return read_description_file(path, tool=tool_name)


@dataclass(frozen=True)
class Fingerprint:
    """``digest`` (sha256 hex) over ``components`` (name -> sha256 hex of that part).

    Components: ``system_prompt``, ``tools``, ``model``, ``sampling``, ``skills``.
    ``digest`` is the sha256 of the sorted ``name=hex`` lines, one per component,
    joined by ``"\n"``.

    ``skills`` covers every byte of the agent's effective skill set (inline,
    workspace, plugin and built-in skills after shadowing and
    ``skills_include``/``skills_exclude``, unavailable and always-on ones included):
    one record per counted file of a file-based skill ``N`` (key
    ``file:N/<path relative to the skill dir>``, value the file's sha256; every
    regular file under the skill directory, symlinks to regular files followed,
    ``__pycache__``, dot paths and ``*.pyc`` skipped) and one per inline skill
    (key ``inline:N``, value the sha256 of the canonical JSON of its ``name``,
    ``description``, ``content`` and ``metadata``). The component is the sha256 of
    ``key + "\n" + value + "\n"`` over the records sorted by key; it never depends
    on absolute paths, modification times or directory order.
    """

    digest: str
    components: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.components, FrozenMap):
            object.__setattr__(self, "components", FrozenMap(self.components))


# Placeholders the fingerprint puts where a per-install / per-rollout path was.
WORKSPACE_PLACEHOLDER = "<workspace>"
SKILLS_PLACEHOLDER = "<builtin-skills>"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sampling_json(sampling: Sampling | None) -> str:
    if sampling is None:
        return "null"
    out: dict[str, Any] = {}
    for f in fields(sampling):
        value = getattr(sampling, f.name)
        if value is None or value == ():
            continue
        if isinstance(value, Mapping):  # logit_bias: int and str keys -> str
            value = {str(k): v for k, v in value.items()}
        out[f.name] = thaw(value)
    return _canonical(out)


def _normaliser(loop: Any) -> list[tuple[str, str]]:
    """``(path text, placeholder)`` pairs, longest path first."""
    pairs: dict[str, str] = {}
    workspace = Path(loop.workspace).expanduser().resolve()
    pairs[str(workspace)] = WORKSPACE_PLACEHOLDER
    skills_root = getattr(getattr(loop.context, "skills", None), "builtin_skills", None)
    if skills_root is not None:
        pairs.setdefault(str(Path(skills_root).expanduser().resolve()), SKILLS_PLACEHOLDER)
    return sorted(pairs.items(), key=lambda item: len(item[0]), reverse=True)


def _normalise(text: str, pairs: list[tuple[str, str]]) -> str:
    for path, placeholder in pairs:
        text = text.replace(path, placeholder)
    return text


def _skill_file_records(name: str, skill_dir: Path) -> list[tuple[str, str]]:
    """``(file:<name>/<rel>, sha256)`` for every counted file under *skill_dir*.

    Raises ``OSError`` when a counted file cannot be read (never a partial digest).
    """
    import os

    records: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(skill_dir, onerror=_raise):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for filename in files:
            if filename.startswith(".") or filename.endswith(".pyc"):
                continue
            path = Path(root) / filename
            if not path.is_file():  # broken symlink, fifo, socket: not a regular file
                continue
            rel = path.relative_to(skill_dir).as_posix()
            with open(path, "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            records.append((f"file:{name}/{rel}", digest))
    return records


def _raise(error: OSError) -> None:
    raise error


def _inline_skill_value(loader: Any, skill: Any) -> str:
    def get(key: str) -> Any:
        value = loader._inline_field(skill, key)
        return thaw(value) if isinstance(value, Mapping) else value

    payload = {key: get(key) for key in ("name", "description", "content", "metadata")}
    return _sha(_canonical(payload))


def skills_component(loop: Any) -> str:
    """The ``skills`` fingerprint component of *loop* (see :class:`Fingerprint`).

    Reads the skill files now (an edit between two calls is reflected); emits no
    ``skill.read``. ``OSError`` when a counted file cannot be read.
    """
    loader = getattr(getattr(loop, "context", None), "skills", None)
    records: list[tuple[str, str]] = []
    if loader is not None:
        for entry in loader.list_skills(filter_unavailable=False):
            name = entry["name"]
            if entry.get("source") == "inline":
                skill = loader.inline_skills.get(name)
                if skill is not None:
                    records.append((f"inline:{name}", _inline_skill_value(loader, skill)))
                continue
            skill_dir = Path(entry["path"]).parent
            records.extend(_skill_file_records(name, skill_dir))
    records.sort(key=lambda record: record[0])
    return _sha("".join(f"{key}\n{value}\n" for key, value in records))


def fingerprint(loop: Any, *, model: str, sampling: Sampling | None) -> Fingerprint:
    """Digest of what *loop*'s model sees: system prompt, tools, model and sampling.

    Normalised, so the digest is stable across builds of the same variant and across
    turns:

    - The system prompt is rendered with no channel, no query, no session summary and
      without the memory and ``# Recent History`` sections (``include_memory=False``,
      ``include_memory_recent_history=False``): those are per-session state that
      changes every turn, not the variant. The system prompt carries no clock; the
      per-turn runtime block (current time, channel, chat id) is appended to the user
      message, so it is not part of the fingerprint at all.
    - In the prompt and in the tool definitions, the resolved workspace path becomes
      ``<workspace>`` and the resolved built-in skills directory ``<builtin-skills>``
      (both differ per install and per rollout temp dir).
    - Kept as-is: the runtime line (OS, arch, Python version), which the model sees.

    ``tools`` is ``loop.tools.get_definitions()`` as canonical JSON (sorted keys);
    ``sampling`` is the canonical JSON of its set fields (``None`` is ``null``, which
    differs from ``Sampling()`` because ``None`` means "the model's default");
    ``skills`` is :func:`skills_component` (every byte of the effective skill set,
    read at call time; ``OSError`` when a counted file cannot be read).
    """
    pairs = _normaliser(loop)
    prompt = loop.context.build_system_prompt(
        include_memory=False, include_memory_recent_history=False, trace=False,
    )
    tools = _canonical(loop.tools.get_definitions())
    components = {
        "system_prompt": _sha(_normalise(prompt, pairs)),
        "tools": _sha(_normalise(tools, pairs)),
        "model": _sha(str(model)),
        "sampling": _sha(_sampling_json(sampling)),
        "skills": skills_component(loop),
    }
    digest = _sha("\n".join(f"{name}={hexd}" for name, hexd in sorted(components.items())))
    return Fingerprint(digest=digest, components=components)


__all__ = ["Fingerprint", "Variant", "fingerprint", "skills_component"]
