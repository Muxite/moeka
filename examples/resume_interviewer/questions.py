"""Interview questions: the six kinds, the schema, the truth lint, candidates and ranking.

The model proposes questions; this module decides what may be asked. Everything a
question must satisfy is checked here, in code:

- ``kind`` is one of :data:`KINDS`;
- ``text`` is one plain sentence ending in ``?``; ``reason`` says why a true answer
  would strengthen the resume;
- ``project`` is ``""`` or an entry the report knows;
- nothing leads the user (no "you should say", no "round it up") and nothing asks to
  drop a truth-bearing qualifier (paper trading, prototype, course, hackathon);
- at most :data:`MAX_QUESTIONS` are asked, ranked by
  must-have weight x strength gain x plausibility.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

KINDS: tuple[str, ...] = (
    "unlock",
    "must_have_probe",
    "stage_scope",
    "number_context",
    "disclosure",
    "library_contradiction",
)
MAX_QUESTIONS = 5
MAX_TEXT = 280
MAX_REASON = 320

# Strength gain if the true answer is "yes", per kind (the middle factor of the rank).
GAIN: Mapping[str, float] = {
    "must_have_probe": 1.0,
    "unlock": 0.9,
    "library_contradiction": 0.8,
    "stage_scope": 0.7,
    "number_context": 0.6,
    "disclosure": 0.5,
}

# Truth-bearing qualifiers: a question may mention them, never ask to lose them.
TRUTH_QUALIFIERS: tuple[str, ...] = (
    "paper trading", "paper-trading", "prototype", "course", "coursework", "class project",
    "hackathon", "simulated", "simulation", "proof of concept", "toy",
)
_DROP_WORDS = re.compile(
    r"\b(drop|dropp(?:ed|ing)|remove|removing|omit|omitting|without|leave out|leaving out|"
    r"hide|hiding|skip the|not mention|call it)\b",
    re.IGNORECASE,
)
# Leading or claim-suggesting phrasing: ask, don't lead.
_LEADING = re.compile(
    r"\b(you should|you could say|we could say|we can say|let'?s say|just say|claim that|"
    r"write that|say that you|pretend|round (?:it |that )?up|exaggerat\w*|inflat\w*|"
    r"wouldn'?t it be fair|isn'?t it true|surely you|you must have|instead of saying)\b",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"[.!?]\s+[A-Z]")


class Question(BaseModel):
    """One interviewer question, in the batch ``questions-out`` shape."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    text: str
    project: str = ""
    reason: str


@dataclass
class Candidate:
    """A question with its rank factors (``score`` = weight x gain x plausibility)."""

    kind: str
    text: str
    project: str
    reason: str
    must_weight: float = 0.3
    plausibility: float = 0.5
    source: str = "report"  # report | seed | model | fallback
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def gain(self) -> float:
        return GAIN.get(self.kind, 0.0)

    @property
    def score(self) -> float:
        return round(self.must_weight * self.gain * self.plausibility, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "text": self.text, "project": self.project,
            "reason": self.reason, "must_weight": self.must_weight,
            "gain": self.gain, "plausibility": self.plausibility, "score": self.score,
            "source": self.source,
        }


# -- validation ----------------------------------------------------------------------


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def question_problems(
    item: Mapping[str, Any], known_projects: Iterable[str] = (),
) -> list[str]:
    """Every rule *item* breaks (empty: it may be asked)."""
    problems: list[str] = []
    kind = _clean(item.get("kind"))
    text = _clean(item.get("text"))
    reason = _clean(item.get("reason"))
    project = _clean(item.get("project"))
    if kind not in KINDS:
        problems.append(f"kind {kind!r} is not one of {list(KINDS)}")
    if not text:
        problems.append("text is empty")
    else:
        if len(text) > MAX_TEXT:
            problems.append(f"text is longer than {MAX_TEXT} characters")
        if not text.endswith("?") or text.count("?") != 1:
            problems.append("text must be one question ending with a single '?'")
        if _SENTENCE_BREAK.search(text):
            problems.append("text must be one sentence")
    if not reason:
        problems.append("reason is empty: say why a true answer strengthens the resume")
    elif len(reason) > MAX_REASON:
        problems.append(f"reason is longer than {MAX_REASON} characters")
    known = {p for p in known_projects if p}
    if project and known and project not in known:
        problems.append(f"project {project!r} is not an entry in the report ({sorted(known)})")
    for label, value in (("text", text), ("reason", reason)):
        if _LEADING.search(value):
            problems.append(f"{label} leads the user; ask what happened, never suggest a claim")
        lowered = value.lower()
        if any(q in lowered for q in TRUTH_QUALIFIERS) and _DROP_WORDS.search(value):
            problems.append(
                f"{label} asks to drop a truth-bearing qualifier (paper trading, prototype, "
                "course, hackathon stay on the page)"
            )
    return problems


def _plausibility(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.05, number))


# -- report reading -------------------------------------------------------------------

_STOP = frozenset(
    "a an and or the of to in on for with by from as at is are be or other related "
    "experience familiarity currently solid skills skill using use any our your you "
    "we will work team program".split()
)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#]+", text.lower()) if len(t) > 1 and t not in _STOP}


def known_projects(report: Mapping[str, Any]) -> list[str]:
    """Entry ids the report shows (bullets, blocked, dropped for fit), in order."""
    seen: dict[str, None] = {}
    for key in ("bullets", "blocked", "dropped_for_fit"):
        for row in report.get(key) or []:
            entry = row.get("entry") if isinstance(row, Mapping) else None
            if isinstance(entry, str) and entry:
                seen.setdefault(entry, None)
    return list(seen)


def requirements(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    posting = report.get("posting") or {}
    return [r for r in posting.get("requirements") or [] if isinstance(r, Mapping)]


def must_weight(text: str, report: Mapping[str, Any]) -> float:
    """1.0 if *text* touches a must-have, 0.6 a nice-to-have, else 0.3."""
    words = _tokens(text)
    best = 0.3
    for req in requirements(report):
        if words & _tokens(str(req.get("text", ""))):
            best = max(best, 1.0 if req.get("kind") == "must" else 0.6)
    return best


def _short(text: str, limit: int = 90) -> str:
    text = _clean(text).rstrip(".")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "..."


_HAS_NUMBER = re.compile(r"\d")
_HAS_CONTEXT = re.compile(r"\bfrom\b.*\bto\b|%|\bper\b|\bbaseline\b|\bvs\.?\b|\bx\b", re.I)
_HAS_USERS = re.compile(r"\b(users?|customers?|clients?|staff|production|deployed|live|adopt\w*)\b", re.I)


def candidates_from_report(report: Mapping[str, Any]) -> list[Candidate]:
    """Deterministic candidates of the four report-derived kinds."""
    out: list[Candidate] = []
    for req in report.get("uncovered_must") or []:
        text = _clean(req.get("text") if isinstance(req, Mapping) else req)
        if not text:
            continue
        out.append(Candidate(
            kind="must_have_probe",
            text=f'Does anything you have done show "{_short(text)}", and if so where?',
            project="",
            reason="It is a must-have of the posting that the resume does not cover yet; "
                   "if it is true for you, one line about it closes the gap.",
            must_weight=1.0, plausibility=0.5, notes={"requirement": req},
        ))
    for row in report.get("blocked") or []:
        if not isinstance(row, Mapping):
            continue
        draft = _clean(row.get("draft"))
        entry = _clean(row.get("entry"))
        if not draft:
            continue
        out.append(Candidate(
            kind="unlock",
            text=f'Did you do this in {entry or "your work"}: "{_short(draft)}"?',
            project=entry,
            reason="The tool blocked this line because your documents do not support it; "
                   "your own account is the only thing that can put it back.",
            must_weight=must_weight(draft, report), plausibility=0.4,
            notes={"blocked": dict(row)},
        ))
    entries_with_users: set[str] = set()
    for row in report.get("bullets") or []:
        if not isinstance(row, Mapping):
            continue
        entry = _clean(row.get("entry"))
        source = _clean(row.get("draft") or row.get("text"))
        if not source:
            continue
        if _HAS_USERS.search(source):
            entries_with_users.add(entry)
        weight = must_weight(source, report)
        if not _HAS_NUMBER.search(source):
            out.append(Candidate(
                kind="unlock",
                text=f'Did you measure any result of this work in {entry or "your work"}: '
                     f'"{_short(source)}"?',
                project=entry,
                reason="A measured result with its baseline would make this a lead line; "
                       "without one it reads as a plain task.",
                must_weight=weight, plausibility=0.45, notes={"fact_key": row.get("fact_key")},
            ))
        elif not _HAS_CONTEXT.search(source):
            out.append(Candidate(
                kind="number_context",
                text=f'What was the baseline or scale behind the number in "{_short(source)}"?',
                project=entry,
                reason="A number with its baseline or scale is believable to a reviewer; "
                       "a bare number invites the question in the interview.",
                must_weight=weight, plausibility=0.6, notes={"fact_key": row.get("fact_key")},
            ))
    for entry in known_projects(report):
        if entry in entries_with_users:
            continue
        out.append(Candidate(
            kind="stage_scope",
            text=f"Was {entry} used by real people, and if so roughly how many?",
            project=entry,
            reason="Real users and their number show the work left the lab, which is the "
                   "strongest signal of scope a project line can carry.",
            must_weight=0.3, plausibility=0.35,
        ))
    return out


def load_seeds(path: Path | None) -> list[Candidate]:
    """Seed questions (JSON list or JSONL of ``{kind, text, project, reason, ...}``).

    Seeds are how ``disclosure`` and ``library_contradiction`` questions come in (for
    example the ambiguities a library review found). They pass the same lint as any
    other question.
    """
    if path is None:
        return []
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        return []
    rows = json.loads(raw) if raw.startswith("[") else [
        json.loads(line) for line in raw.splitlines() if line.strip()
    ]
    out = []
    for row in rows:
        out.append(Candidate(
            kind=_clean(row.get("kind")), text=_clean(row.get("text")),
            project=_clean(row.get("project")), reason=_clean(row.get("reason")),
            must_weight=_plausibility(row.get("must_weight"), 0.6),
            plausibility=_plausibility(row.get("plausibility"), 0.6), source="seed",
        ))
    return out


def candidate_from_model(item: Mapping[str, Any], report: Mapping[str, Any]) -> Candidate:
    text = _clean(item.get("text"))
    kind = _clean(item.get("kind"))
    weight = 1.0 if kind == "must_have_probe" else must_weight(
        f"{text} {_clean(item.get('reason'))}", report)
    return Candidate(
        kind=kind, text=text, project=_clean(item.get("project")),
        reason=_clean(item.get("reason")), must_weight=weight,
        plausibility=_plausibility(item.get("plausibility")), source="model",
    )


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def select(
    candidates: Sequence[Candidate], projects: Iterable[str], *, cap: int = MAX_QUESTIONS,
) -> tuple[list[Question], list[dict[str, Any]]]:
    """Lint, dedupe, rank and cap; return (questions ``q1..``, rejections)."""
    cap = max(0, min(int(cap), MAX_QUESTIONS))
    projects = list(projects)
    rejected: list[dict[str, Any]] = []
    valid: list[Candidate] = []
    seen: set[str] = set()
    for cand in candidates:
        problems = question_problems(
            {"kind": cand.kind, "text": cand.text, "reason": cand.reason,
             "project": cand.project}, projects,
        )
        if problems:
            rejected.append({"text": cand.text, "kind": cand.kind, "problems": problems})
            continue
        key = _norm(cand.text)
        if key in seen:
            rejected.append({"text": cand.text, "kind": cand.kind, "problems": ["duplicate"]})
            continue
        seen.add(key)
        valid.append(cand)
    ranked = sorted(enumerate(valid), key=lambda iv: (-iv[1].score, iv[0]))
    chosen = [c for _, c in ranked[:cap]]
    for _, cand in ranked[cap:]:
        rejected.append({"text": cand.text, "kind": cand.kind,
                         "problems": [f"over the cap of {cap} questions"]})
    questions = [
        Question(id=f"q{i}", kind=c.kind, text=c.text, project=c.project, reason=c.reason)
        for i, c in enumerate(chosen, 1)
    ]
    return questions, rejected
