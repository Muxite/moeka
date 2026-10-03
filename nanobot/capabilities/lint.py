"""Static lint for skill text (spec 010 FR-002).

``lint_text(text)`` returns one :class:`Finding` per match of four patterns that a skill
should never teach:

- ``bypass-language``: ``bypass``, ``circumvent`` or ``evade`` within 40 characters of
  ``allowlist``, ``denylist``, ``deny pattern``, ``filter``, ``sandbox``, ``guard``,
  ``restriction`` or ``permission`` (case-insensitive, either order). Stems are matched,
  so ``bypassing`` and ``filters`` count;
- ``pipe-to-shell``: ``curl``/``wget`` ... ``| sh|bash`` or ``base64 -d`` ... ``| sh|bash``
  on one line;
- ``sudo-stdin``: ``sudo -S`` (a password on stdin);
- ``secret-path``: ``keys.env``, ``.ssh/id_``, ``auth.json``.

Lint is a hint, not containment: it catches what Dream wrote on 2026-10-03 (four skills
teaching how to get around the command allowlist), not a determined author.

Kernel code: imports no host package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BYPASS_LANGUAGE = "bypass-language"
PIPE_TO_SHELL = "pipe-to-shell"
SUDO_STDIN = "sudo-stdin"
SECRET_PATH = "secret-path"
FINDING_CODES = (BYPASS_LANGUAGE, PIPE_TO_SHELL, SUDO_STDIN, SECRET_PATH)

_WINDOW = 40
_EXCERPT_CHARS = 160
_EVASION = re.compile(r"bypass|circumvent|evad(?:e|es|ed|ing)", re.IGNORECASE)
_CONTROL = re.compile(
    r"allowlist|allow-list|denylist|deny-list|deny[\s_-]?pattern|filter|sandbox|guard"
    r"|restrict|permission",
    re.IGNORECASE,
)
_LINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (PIPE_TO_SHELL, re.compile(
        r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"
        r"|\bbase64\s+(?:-\w*d\w*|--decode)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b",
    )),
    (SUDO_STDIN, re.compile(
        r"\bsudo(?:[ \t]+-[A-Za-z]+(?:[ \t]+[^-\s]\S*)?)*?[ \t]+(?:-[A-Za-z]*S[A-Za-z]*|--stdin)\b",
    )),
    (SECRET_PATH, re.compile(r"keys\.env|\.ssh/id_|auth\.json")),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One lint hit: the finding code, its 1-based line and the stripped line text."""

    code: str
    line: int
    excerpt: str


def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(i + 1 for i, ch in enumerate(text) if ch == "\n")
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid
    return lo + 1


def _excerpt(lines: list[str], line: int) -> str:
    text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return text if len(text) <= _EXCERPT_CHARS else text[: _EXCERPT_CHARS - 3] + "..."


def lint_text(text: str) -> list[Finding]:
    """Findings for *text*, ordered by line then code; one per (code, line)."""
    starts = _line_starts(text)
    lines = text.split("\n")
    hits: set[tuple[int, str]] = set()
    controls = [(m.start(), m.end()) for m in _CONTROL.finditer(text)]
    for match in _EVASION.finditer(text):
        for start, end in controls:
            gap = start - match.end() if start >= match.end() else match.start() - end
            if gap <= _WINDOW:
                hits.add((_line_of(starts, match.start()), BYPASS_LANGUAGE))
                break
    for number, line in enumerate(lines, start=1):
        for code, pattern in _LINE_PATTERNS:
            if pattern.search(line):
                hits.add((number, code))
    order = {code: i for i, code in enumerate(FINDING_CODES)}
    return [
        Finding(code=code, line=line, excerpt=_excerpt(lines, line))
        for line, code in sorted(hits, key=lambda hit: (hit[0], order[hit[1]]))
    ]


def lint_file(path: str | Path) -> list[Finding]:
    """Findings for one file (an unreadable file has none: validation reports it)."""
    try:
        return lint_text(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def finding_codes(findings: list[Finding]) -> list[str]:
    """The distinct codes of *findings*, in :data:`FINDING_CODES` order."""
    present = {finding.code for finding in findings}
    return [code for code in FINDING_CODES if code in present]


__all__ = [
    "BYPASS_LANGUAGE",
    "FINDING_CODES",
    "Finding",
    "PIPE_TO_SHELL",
    "SECRET_PATH",
    "SUDO_STDIN",
    "finding_codes",
    "lint_file",
    "lint_text",
]
