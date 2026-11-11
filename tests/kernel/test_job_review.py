"""Evidence rule and downtime review (spec 015 phase 4)."""

import pytest

from nanobot.kernel.job_review import (
    MIN_REUSE,
    find_failure_clusters,
    find_repeats,
    new_skill_gate,
    signature,
)
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def _done(store, text, n=1):
    for _ in range(n):
        j = store.open_job(session_key="s", request_text=text)
        store.transition(j.id, JobState.DONE)


def test_signature_ignores_filler_digits_and_case():
    assert signature("Please open port 8080 on tailscale") == signature("open port 9090 on Tailscale")
    assert signature("hi") == ""


def test_repeats_need_the_count_and_only_count_done_jobs(store):
    _done(store, "open a port on tailscale for the demo", 2)
    failed = store.open_job(session_key="s", request_text="open a port on tailscale for the demo")
    store.transition(failed.id, JobState.FAILED, side=FailureSide.MOEKA, cause="x")
    (c,) = find_repeats(store, min_count=2)
    assert c.count == 2 and "tailscale" in c.key
    assert find_repeats(store, min_count=3) == []


def test_failure_clusters_group_by_side_and_cause(store):
    for _ in range(3):
        j = store.open_job(session_key="s", request_text="fix it", role="worker")
        store.transition(j.id, JobState.FAILED, side=FailureSide.WORKER, cause="claude:usage_limit")
    (c,) = find_failure_clusters(store, min_count=3)
    assert c.key == "worker:claude:usage_limit" and c.count == 3


def test_new_skill_is_refused_without_evidence_and_says_why(store):
    _done(store, "rotate the nightly backup keys", MIN_REUSE - 1)
    reason = new_skill_gate(store, "rotate-keys")
    assert reason and "/approve-skill" in reason and str(MIN_REUSE) in reason


def test_enough_reuse_allows_exactly_one_skill_per_pattern(store):
    _done(store, "rotate the nightly backup keys", MIN_REUSE)
    assert new_skill_gate(store, "rotate-keys") is None
    assert new_skill_gate(store, "rotate-keys-2") is not None  # same evidence is not reusable


def test_owner_approval_allows_one_and_cannot_be_self_granted_by_the_gate(store):
    assert new_skill_gate(store, "a") is not None
    store.add_owner_approval("yes, weekly")
    assert new_skill_gate(store, "a") is None
    assert new_skill_gate(store, "b") is not None
    assert store.unused_owner_approvals() == 0


def test_review_questions_are_asked_once(store):
    assert not store.was_asked("repeat:x")
    store.mark_asked("repeat:x", "repeat")
    assert store.was_asked("repeat:x")
