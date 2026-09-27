"""Compile an :class:`~nanobot.config.schema.AgentProfileConfig` into loop inputs.

Shared by ``MoekaCore.create(profile=...)`` and the kernel's ``Agent`` (which turns
an ``AgentSpec`` into a profile): the profile scopes a deep copy of the config's
``agents.defaults``, and its persona / inline skills become the loop's in-memory
bootstrap overrides and inline skills. Nothing is written to disk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def apply_profile(cfg: Any, profile: Any) -> tuple[Any, Any]:
    """Compile a profile into a deep-copied config's agents.defaults.

    *profile* may be a name from ``config.profiles``, an
    :class:`~nanobot.config.schema.AgentProfileConfig` instance, or a plain
    dict — the latter two let embedding hosts define their scope in code
    without touching the user's config file.
    """
    from nanobot.config.schema import AgentProfileConfig

    if isinstance(profile, str):
        prof = cfg.resolve_profile(profile)
    elif isinstance(profile, AgentProfileConfig):
        prof = profile
    else:
        prof = AgentProfileConfig.model_validate(profile)
    cfg = cfg.model_copy(deep=True)
    d = cfg.agents.defaults
    if prof.model_preset:
        d.model_preset = prof.model_preset
    if prof.tools_allow is not None:
        d.tools_allow = list(prof.tools_allow)
    if prof.tools_deny:
        d.tools_deny = sorted({*d.tools_deny, *prof.tools_deny})
    if prof.skills_include is not None:
        d.allowed_skills = list(prof.skills_include)
    if prof.skills_exclude:
        d.disabled_skills = sorted({*d.disabled_skills, *prof.skills_exclude})
    if not prof.memory_enabled:
        d.vec.enable = False
    if prof.planning:
        d.planning = True
    if prof.limits is not None:
        d.limits = prof.limits
    return cfg, prof


def build_bootstrap_overrides(
    prof: Any, bootstrap: Mapping[str, str] | None
) -> dict[str, str] | None:
    """Merge the profile persona and explicit bootstrap sections — in memory.

    Explicit ``bootstrap`` entries win; the persona fills ``"AGENTS.md"``
    only when the caller didn't supply that key. ``system_prompt_file`` is
    read here, once — the path is the host's choice, the core sees content.
    """
    overrides = dict(bootstrap or {})
    if prof is not None and "AGENTS.md" not in overrides:
        text = prof.system_prompt
        if not text and prof.system_prompt_file:
            text = read_system_prompt_file(prof.system_prompt_file)
        if text:
            overrides["AGENTS.md"] = text
    return overrides or None


def read_system_prompt_file(path: str) -> str:
    """The persona text of a profile's ``system_prompt_file`` (``~`` expanded)."""
    return Path(path).expanduser().read_text(encoding="utf-8")


def build_inline_skills(prof: Any, skills: Sequence[Any] | None) -> list[Any] | None:
    """Combine profile ``skills_inline`` with create-time ``skills`` (validated)."""
    from nanobot.config.schema import InlineSkillConfig

    combined = list(getattr(prof, "skills_inline", None) or [])
    for skill in skills or []:
        if isinstance(skill, InlineSkillConfig):
            combined.append(skill)
        else:
            combined.append(InlineSkillConfig.model_validate(skill))
    return combined or None


__all__ = [
    "apply_profile",
    "build_bootstrap_overrides",
    "build_inline_skills",
    "read_system_prompt_file",
]
