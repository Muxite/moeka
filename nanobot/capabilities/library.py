"""Index entries from the live skill and tool lists (spec 011 FR-004, FR-015).

``skill_entries`` reads a ``SkillsLoader``'s listing (today's order, unavailable skills
included); ``tool_entries`` a tool registry's tools. Both compute ``content_sha256``
as FR-004 defines it, and the spec 012 provenance columns (workspace skills: the
loader's certificate gate; script tools: their ``cert_status``; the rest ``builtin``). A skill whose ``io`` block does not parse (only possible for a
built-in or plugin skill: workspace skills are validated by the loader) is indexed
untyped, so it is never hidden.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.capabilities.index import CapabilityEntry
from nanobot.capabilities.manifest import IOSpec, ManifestError, parse_io

if TYPE_CHECKING:
    from nanobot.agent.skills import SkillsLoader
    from nanobot.agent.tools.base import Tool


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _skill_io(name: str, raw: object) -> IOSpec | None:
    try:
        return parse_io(raw)
    except ManifestError as exc:
        logger.debug("capabilities: skill '{}' has an invalid io block ({}); untyped", name, exc)
        return None


def skill_entries(loader: SkillsLoader) -> list[CapabilityEntry]:
    """One entry per listed skill, in ``list_skills`` order."""
    entries: list[CapabilityEntry] = []
    for item in loader.list_skills(filter_unavailable=False):
        name = item["name"]
        metadata = loader.get_skill_metadata(name) or {}
        if item.get("source") == "inline":
            skill = loader.inline_skills.get(name)
            payload = {
                key: loader._inline_field(skill, key)
                for key in ("name", "description", "content", "metadata")
            }
            sha = _sha(_canonical(payload).encode("utf-8"))
        else:
            try:
                sha = _sha(Path(item["path"]).read_bytes())
            except OSError:
                continue
        # One metadata read per skill (the loader's helpers would each re-read it).
        description = metadata.get("description")
        if not isinstance(description, str) or not description:
            description = name
        available = loader._check_requirements(
            loader._parse_nanobot_metadata(metadata.get("metadata"))
        )
        cert_id, cert_status = None, "builtin"
        if item.get("source") == "workspace":
            provenance = loader._workspace_provenance(Path(item["path"]).parent)
            cert_id, cert_status = provenance.cert_id, provenance.status
        entries.append(CapabilityEntry(
            kind="skill",
            name=name,
            description=description,
            io=_skill_io(name, metadata.get("io")),
            content_sha256=sha,
            available=available,
            cert_id=cert_id,
            cert_status=cert_status,
        ))
    return entries


def tool_sha256(tool: Tool) -> str:
    """sha256 of ``name + description + json(parameters) + json(io)`` (FR-004)."""
    io = getattr(tool, "io", None)
    text = tool.name + tool.description + _canonical(tool.parameters) + _canonical(io)
    return _sha(text.encode("utf-8"))


def tool_entries(tools: Iterable[Tool]) -> list[CapabilityEntry]:
    """One entry per tool; a tool with an invalid ``io`` attribute is indexed untyped."""
    entries: list[CapabilityEntry] = []
    for tool in tools:
        try:
            io = parse_io(getattr(tool, "io", None))
        except ManifestError as exc:
            logger.warning(
                "capabilities: tool '{}' has an invalid io ({}); untyped", tool.name, exc,
            )
            io = None
        cert_status = getattr(tool, "cert_status", None)
        entries.append(CapabilityEntry(
            kind="tool",
            name=tool.name,
            description=tool.description,
            io=io,
            content_sha256=tool_sha256(tool),
            available=True,
            cert_id=getattr(tool, "cert_id", None) if isinstance(cert_status, str) else None,
            cert_status=cert_status if isinstance(cert_status, str) else "builtin",
        ))
    return entries


def entries_signature(entries: Iterable[CapabilityEntry]) -> str:
    """A digest of an entry list: equal lists give equal signatures (cheap change check)."""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            f"{entry.kind}\0{entry.name}\0{entry.content_sha256}\0{int(entry.available)}"
            f"\0{entry.cert_id}\0{entry.cert_status}\n".encode()
        )
    return digest.hexdigest()


__all__ = ["entries_signature", "skill_entries", "tool_entries", "tool_sha256"]
