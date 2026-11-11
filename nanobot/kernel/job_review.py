"""Downtime review over the job record, and the evidence rule for new skills (spec 015 phase 4).

Agents favour building tools whether or not reuse will occur. This module makes the proof a
code rule, not a prompt: a NEW skill is only staged when the record shows the same kind of
request completed at least ``MIN_REUSE`` times, or the owner approved one with
``/approve-skill``. The model cannot grant itself an approval: that is an owner slash
command with no tool. Stdlib only (kernel import rule).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from nanobot.kernel.jobs import Job, JobState, JobStore

MIN_REUSE = 3
_STOP = frozenset(
    "a an the to for of in on at and or please can you could would me my we our it is this "
    "that with from by be do does did just now again hey hi".split()
)
_WORD = re.compile(r"[a-z][a-z0-9_-]+")


def signature(text: str, words: int = 6) -> str:
    """A stable key for "the same kind of request": first meaningful words, order kept."""
    found = [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]
    return " ".join(found[:words])


@dataclass(slots=True)
class Cluster:
    kind: str  # "repeat" | "failure"
    key: str
    count: int
    job_ids: list[str] = field(default_factory=list)
    sample: str = ""


def _chat_and_worker(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.role in ("", "worker")]


def find_repeats(store: JobStore, *, since: str | None = None, min_count: int = 2) -> list[Cluster]:
    """Request signatures that completed ``min_count``+ times (done jobs only)."""
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in _chat_and_worker(store.list(states={JobState.DONE}, since=since, limit=500)):
        sig = signature(job.request_text)
        if len(sig.split()) >= 2:
            groups[sig].append(job)
    out = [
        Cluster("repeat", sig, len(js), [j.id for j in js], js[0].request_text[:120])
        for sig, js in groups.items() if len(js) >= min_count
    ]
    return sorted(out, key=lambda c: (-c.count, c.key))


def find_failure_clusters(
    store: JobStore, *, since: str | None = None, min_count: int = 3,
) -> list[Cluster]:
    """The same side and cause stopping jobs ``min_count``+ times."""
    groups: dict[str, list[Job]] = defaultdict(list)
    bad = {JobState.FAILED, JobState.BLOCKED, JobState.OUTCOME_UNKNOWN}
    for job in store.list(states=bad, since=since, limit=500):
        stop = next((e for e in reversed(store.history(job.id)) if e.cause), None)
        if stop is not None and stop.side is not None:
            groups[f"{stop.side.value}:{stop.cause}"].append(job)
    out = [
        Cluster("failure", key, len(js), [j.id for j in js], js[0].request_text[:120])
        for key, js in groups.items() if len(js) >= min_count
    ]
    return sorted(out, key=lambda c: (-c.count, c.key))


def new_skill_gate(store: JobStore, name: str) -> str | None:
    """``None`` to allow staging a new skill called *name*, else the refusal to show the agent.

    Spends one unit of evidence: an unconsumed repeat cluster (>= MIN_REUSE completed jobs),
    else one owner approval.
    """
    spent = store.consumed_bases()
    for cluster in find_repeats(store, min_count=MIN_REUSE):
        if cluster.key not in spent:
            store.record_skill_proposal(name, cluster.key)
            return None
    if store.use_owner_approval(name):
        store.record_skill_proposal(name, f"owner:{name}")
        return None
    return (
        f"No new skill {name!r} yet: the job record shows no request completed at least "
        f"{MIN_REUSE} times that is not already covered by a skill, and the owner has not "
        "approved one. Write what you learned into memory or improve an existing skill "
        "instead, or ask the owner whether this recurring task is worth a skill "
        "(they can allow it with /approve-skill)."
    )
