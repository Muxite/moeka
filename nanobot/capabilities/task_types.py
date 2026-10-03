"""Deterministic task typing (spec 011 FR-005).

:func:`extract` maps a turn's text and attachments to vocabulary types and an optional
requested output type. It is a pure function of its arguments: no model call, no
network, no filesystem access. Ambient types (``text/query``, ``workspace``, ...) are
not added here; the selector adds them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

from nanobot.capabilities.types import is_type

# File extension -> type (attachments, URL paths and filesystem paths share it).
EXTENSION_TYPES: dict[str, str] = {
    ".pdf": "file:pdf",
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp"), "file:image"),
    **dict.fromkeys((".mp3", ".wav", ".ogg", ".m4a", ".flac"), "file:audio"),
    **dict.fromkeys((".mp4", ".mkv", ".mov", ".webm"), "file:video"),
    **dict.fromkeys((".zip", ".tar", ".gz", ".tgz", ".7z"), "file:archive"),
    **dict.fromkeys(
        (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt"), "file:office",
    ),
    ".csv": "data/csv",
    ".json": "data/json",
    ".yaml": "data/yaml",
    ".yml": "data/yaml",
    ".md": "text/markdown",
    ".py": "text/code:python",
    ".sh": "text/code:shell",
    ".log": "text/log",
}

_MIME_PREFIX_TYPES = (("image/", "file:image"), ("audio/", "file:audio"), ("video/", "file:video"))
_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
_FENCE_TYPES = {
    "python": "text/code:python",
    "bash": "text/code:shell",
    "sh": "text/code:shell",
    "shell": "text/code:shell",
    "json": "data/json",
}
_OUTPUT_WORDS = {
    "csv": "data/csv",
    "json": "data/json",
    "yaml": "data/yaml",
    "markdown": "text/markdown",
    "pdf": "file:pdf",
    "table": "data/csv",
    "chart": "file:image",
    "image": "file:image",
}

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
# An absolute (``/...``) or home-relative (``~/...``) path not glued to a preceding word.
_PATH = re.compile(r"(?<![\w/~.:\-])(~/[^\s<>\"'`|;,()\[\]{}]*|/[^\s<>\"'`|;,()\[\]{}/][^\s<>\"'`|;,()\[\]{}]*)")
# A fence: three backticks and an optional language; fences alternate open/close.
_FENCE = re.compile(r"```([A-Za-z0-9_+.\-]*)")
_HINT = re.compile(r"\$type:([A-Za-z0-9_/:.\-]+)")
_OUT_HINT = re.compile(r"\$out:([A-Za-z0-9_/:.\-]+)")
_REQUESTED = re.compile(
    r"\b(?:as|to|into|in)\s+(?:a\s+)?(csv|json|yaml|markdown|pdf|table|chart|image)\b",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}'\""


@dataclass(frozen=True)
class TaskTyping:
    """The types extracted from one turn and the requested output (``None`` = any)."""

    types: frozenset[str]
    requested_output: str | None = None


@dataclass(frozen=True)
class Attachment:
    """A turn attachment as :func:`extract` sees it: a file name and/or a MIME type."""

    name: str | None = None
    mime: str | None = None


def _extension_type(name: str) -> str | None:
    suffix = PurePosixPath(name).suffix.lower()
    return EXTENSION_TYPES.get(suffix) if suffix else None


def _attachment_field(attachment: Any, key: str) -> str | None:
    value = attachment.get(key) if isinstance(attachment, dict) else getattr(attachment, key, None)
    return value if isinstance(value, str) and value else None


def _attachment_types(attachment: Any) -> set[str]:
    if isinstance(attachment, str):  # a bare file name or path
        attachment = Attachment(name=attachment)
    found: set[str] = set()
    mime = (_attachment_field(attachment, "mime") or "").lower().split(";")[0].strip()
    if mime == "application/pdf":
        found.add("file:pdf")
    for prefix, type_name in _MIME_PREFIX_TYPES:
        if mime.startswith(prefix):
            found.add(type_name)
    name = _attachment_field(attachment, "name")
    if name and (by_ext := _extension_type(name)):
        found.add(by_ext)
    return found


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _url_types(url: str) -> set[str]:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return {"url/web"}
    path = parts.path
    found: set[str] = set()
    if _host_matches(host, _GIT_HOSTS) or path.lower().endswith(".git"):
        found.add("url/git")
    elif _host_matches(host, _VIDEO_HOSTS):
        found.add("url/video")
    else:
        found.add("url/web")
    if by_ext := _extension_type(path):
        found.add(by_ext)
    return found


def _fence_types(text: str) -> set[str]:
    found: set[str] = set()
    opening = True
    for match in _FENCE.finditer(text):
        if opening:
            language = match.group(1).lower()
            found.add(_FENCE_TYPES.get(language, "text/code"))
        opening = not opening
    return found


def extract(text: str, attachments: Iterable[Any] = ()) -> TaskTyping:
    """Types of a turn from *text* and *attachments* (objects with ``.mime``/``.name``)."""
    text = text or ""
    found: set[str] = set()
    for attachment in attachments or ():
        found |= _attachment_types(attachment)

    for match in _URL.finditer(text):
        found |= _url_types(match.group(0).rstrip(_TRAILING))
    # Paths are looked for outside URLs only (a URL's path is not a filesystem path).
    without_urls = _URL.sub(" ", text)
    for match in _PATH.finditer(without_urls):
        path = match.group(1).rstrip(_TRAILING)
        if not path or path in ("/", "~"):
            continue
        found.add("path")
        if by_ext := _extension_type(path):
            found.add(by_ext)

    found |= _fence_types(text)
    for match in _HINT.finditer(text):
        hint = match.group(1).rstrip(_TRAILING)
        if is_type(hint):
            found.add(hint)

    requested: str | None = None
    for match in _REQUESTED.finditer(text):
        requested = _OUTPUT_WORDS[match.group(1).lower()]
    for match in _OUT_HINT.finditer(text):
        hint = match.group(1).rstrip(_TRAILING)
        if is_type(hint):
            requested = hint
    return TaskTyping(types=frozenset(found), requested_output=requested)


__all__ = ["EXTENSION_TYPES", "Attachment", "TaskTyping", "extract"]
