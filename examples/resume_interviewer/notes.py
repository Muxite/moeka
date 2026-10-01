"""The answers notes doc (``W/docs/answers-YYYYMMDD.md``), report summaries and diffs.

awr takes every document in ``W/docs`` as true, so a dated notes doc is how the
user's content answers reach the next build. Each answer section carries a marker
with the run, the question id and the kernel fact id it was recorded as
(``source="user"``), so every new line traces back to the user's own words.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

# Answers that add nothing to the resume: recorded as facts, not written to the doc.
_EMPTY_ANSWER = re.compile(r"^\s*(no|nope|n/?a|none|skip|pass|not really|i don'?t know|idk)\.?\s*$",
                           re.IGNORECASE)
_MARKER = re.compile(r"<!-- resume-interviewer: run=(\S+) id=(\S+) .*?-->")


def is_content_answer(answer: str) -> bool:
    return bool(answer.strip()) and not _EMPTY_ANSWER.match(answer)


def notes_path(workspace: Path, day: _dt.date | None = None) -> Path:
    day = day or _dt.date.today()
    return Path(workspace) / "docs" / f"answers-{day:%Y%m%d}.md"


def _section(run_id: str, row: Mapping[str, Any]) -> str:
    project = row.get("project") or ""
    heading = f"{project}: {row['text']}" if project else row["text"]
    return (
        f"## {heading}\n"
        f"<!-- resume-interviewer: run={run_id} id={row['id']} kind={row['kind']} "
        f"fact={row.get('fact_id') or '-'} source=user -->\n\n"
        f"Answer from the user: {' '.join(str(row['answer']).split())}\n"
    )


def write_notes(
    workspace: Path, run_id: str, rows: Sequence[Mapping[str, Any]], *,
    day: _dt.date | None = None,
) -> Path | None:
    """Add or replace one section per content answer; return the doc (None: nothing to write).

    ``rows`` carry ``id``, ``kind``, ``text``, ``project``, ``answer`` and ``fact_id``.
    Writing the same answers again changes nothing (sections are keyed by run and id).
    """
    rows = [r for r in rows if is_content_answer(str(r.get("answer", "")))]
    if not rows:
        return None
    path = notes_path(workspace, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    day = day or _dt.date.today()
    header = (
        f"# Interview answers (user-provided, {day:%Y-%m-%d})\n\n"
        "These are the user's own answers to resume interview questions. They are taken "
        "as true, and the user is responsible for them.\n"
    )
    sections: dict[tuple[str, str], str] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"(?m)^(?=## )", text)
        header = parts[0] if parts and not parts[0].startswith("## ") else header
        for part in parts:
            match = _MARKER.search(part)
            if part.startswith("## ") and match:
                sections[(match.group(1), match.group(2))] = part.rstrip("\n") + "\n"
    for row in rows:
        sections[(run_id, str(row["id"]))] = _section(run_id, row)
    body = header.rstrip("\n") + "\n\n" + "\n".join(sections.values())
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return path


# -- summaries and diffs ----------------------------------------------------------------


def report_summary(report: Mapping[str, Any], envelope: Mapping[str, Any] | None = None) -> dict:
    """What the agent needs from a build: compact, no file paths."""
    data = (envelope or {}).get("data") or {}
    return {
        "exit_code": (envelope or {}).get("exit_code"),
        "posting": {
            "title": (report.get("posting") or {}).get("title"),
            "company": (report.get("posting") or {}).get("company"),
            "requirements": [
                {"kind": r.get("kind"), "text": r.get("text")}
                for r in (report.get("posting") or {}).get("requirements") or []
            ],
        },
        "bullets": [
            {"entry": b.get("entry"), "text": b.get("text"), "status": b.get("status"),
             "flags": b.get("flags")}
            for b in report.get("bullets") or []
        ],
        "blocked": [
            {"entry": b.get("entry"), "draft": b.get("draft"), "flags": b.get("flags")}
            for b in report.get("blocked") or []
        ],
        "uncovered_must": [u.get("text") for u in report.get("uncovered_must") or []],
        "dropped_for_fit": report.get("dropped_for_fit") or [],
        "omitted_sections": report.get("omitted_sections") or [],
        "header_placeholders": report.get("header_placeholders") or [],
        "questions_open": data.get("questions_open"),
    }


def _bullet_key(row: Mapping[str, Any]) -> str:
    return str(row.get("fact_key") or f"{row.get('entry')}|{row.get('text')}")


def _texts(rows: Iterable[Any]) -> list[str]:
    return [str(r.get("text") if isinstance(r, Mapping) else r) for r in rows]


def report_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """uncovered_must, bullets and blocked changes between two reports."""
    unc_b = _texts(before.get("uncovered_must") or [])
    unc_a = _texts(after.get("uncovered_must") or [])
    bul_b = {_bullet_key(b): b for b in before.get("bullets") or []}
    bul_a = {_bullet_key(b): b for b in after.get("bullets") or []}
    blk_b = {_bullet_key(b) + "|" + str(b.get("draft")): b for b in before.get("blocked") or []}
    blk_a = {_bullet_key(b) + "|" + str(b.get("draft")): b for b in after.get("blocked") or []}

    def brief(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"fact_key": row.get("fact_key"), "entry": row.get("entry"),
                "text": row.get("text") or row.get("draft"), "status": row.get("status"),
                "flags": row.get("flags")}

    return {
        "uncovered_must": {
            "before": unc_b, "after": unc_a,
            "resolved": [u for u in unc_b if u not in unc_a],
            "new": [u for u in unc_a if u not in unc_b],
        },
        "bullets": {
            "added": [brief(bul_a[k]) for k in bul_a if k not in bul_b],
            "removed": [brief(bul_b[k]) for k in bul_b if k not in bul_a],
            "changed": [
                {"fact_key": k, "entry": bul_a[k].get("entry"),
                 "before": bul_b[k].get("text"), "after": bul_a[k].get("text"),
                 "status_before": bul_b[k].get("status"), "status_after": bul_a[k].get("status")}
                for k in bul_a if k in bul_b and (
                    bul_a[k].get("text") != bul_b[k].get("text")
                    or bul_a[k].get("status") != bul_b[k].get("status"))
            ],
        },
        "blocked": {
            "added": [brief(blk_a[k]) for k in blk_a if k not in blk_b],
            "removed": [brief(blk_b[k]) for k in blk_b if k not in blk_a],
        },
        "header_placeholders": {
            "before": list(before.get("header_placeholders") or []),
            "after": list(after.get("header_placeholders") or []),
        },
    }
