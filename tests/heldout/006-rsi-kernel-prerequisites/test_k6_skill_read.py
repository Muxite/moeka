"""K6: ``skill.read`` coverage (FR-035 to FR-040, SC-006, US5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _h006 import calls, ev, strip, tc, write_skill

from moeka.agents import AgentSpec
from moeka.testing import FakeProvider
from moeka.tools import DefaultPolicy
from moeka.variants import Variant

SKILL_READ_KEYS = {"skill", "path", "via", "call_id"}


def _ws(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    a = write_skill(work / "skills", "alpha", "alpha line one NEEDLE\nalpha two NEEDLE\nend")
    (a / "notes.md").write_text("notes NEEDLE in a supporting file\n")
    (a / "sub").mkdir()
    (a / "sub" / "SKILL.md").write_text("nested NEEDLE not a skill file\n")
    write_skill(work / "skills", "beta", "beta NEEDLE body")
    write_skill(work / "skills", "quiet", "nothing to find here")
    (work / "skills" / "SKILL.md").write_text("NEEDLE directly under skills\n")
    (work / "docs").mkdir()
    (work / "docs" / "SKILL.md").write_text("NEEDLE outside a skills root\n")
    (work / "other.txt").write_text("NEEDLE plain file\n")
    return work


def _run(kmaker, script, *, variant=None, **kw):
    fake = FakeProvider(script, default="fallback")
    kernel = kmaker(fake, variant=variant)
    agent = kernel.agent(AgentSpec(name=kw.pop("name", "k6"), **kw))
    return agent.run_sync("go"), fake


def _index(sink, pred) -> list[int]:
    return [i for i, e in enumerate(sink.events) if pred(e)]


def _assert_before_tool_call(sink, call_id: str) -> None:
    reads = _index(sink, lambda e: e.get("event") == "skill.read" and e.get("call_id") == call_id)
    [call] = _index(sink, lambda e: e.get("event") == "tool.call" and e.get("call_id") == call_id)
    assert reads and all(i < call for i in reads)


def _resolved(p: Path) -> str:
    return str(p.resolve())


# -- read_file ------------------------------------------------------------------------------


@pytest.mark.fr("FR-035", "FR-036", "FR-037")
def test_read_file_emits_with_via_and_call_id(kmaker, sink, tmp_path):
    work = _ws(tmp_path)
    result, _ = _run(kmaker, [tc("read_file", {"path": "skills/alpha/SKILL.md"}, "r1"), "done"])
    assert result.stop_reason == "completed"
    [read] = ev(sink, "skill.read")
    assert set(strip(read)) == SKILL_READ_KEYS
    assert read["skill"] == "alpha"
    assert read["path"] == _resolved(work / "skills" / "alpha" / "SKILL.md")
    assert read["via"] == "read_file" and read["call_id"] == "r1"
    [call] = [e for e in ev(sink, "tool.call") if e["call_id"] == "r1"]
    assert call["ok"] is True
    _assert_before_tool_call(sink, "r1")


@pytest.mark.fr("FR-035", "FR-037")
def test_read_file_range_emits(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [tc("read_file", {"path": "skills/beta/SKILL.md", "offset": 2, "limit": 1},
                     "r2"), "done"])
    [read] = ev(sink, "skill.read")
    assert (read["skill"], read["via"], read["call_id"]) == ("beta", "read_file", "r2")


@pytest.mark.fr("FR-035", "FR-036", "FR-037")
def test_two_reads_in_one_response(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [calls(("read_file", {"path": "skills/alpha/SKILL.md"}, "m1"),
                        ("read_file", {"path": "skills/beta/SKILL.md"}, "m2")), "done"])
    reads = ev(sink, "skill.read")
    assert sorted((e["call_id"], e["skill"]) for e in reads) == [("m1", "alpha"), ("m2", "beta")]
    _assert_before_tool_call(sink, "m1")
    _assert_before_tool_call(sink, "m2")


@pytest.mark.fr("FR-035", "FR-036")
def test_read_file_builtin_root_skill(kmaker, sink, tmp_path):
    root = tmp_path / "variant-skill-root"
    write_skill(root, "gadget", "Gadget body.")
    result, _ = _run(kmaker, [tc("read_file", {"path": "skills/gadget/SKILL.md"}, "v1"), "done"],
                     variant=Variant(builtin_skills_dir=root))
    [read] = ev(sink, "skill.read")
    assert (read["skill"], read["via"], read["call_id"]) == ("gadget", "read_file", "v1")
    assert read["path"] == _resolved(root / "gadget" / "SKILL.md")


@pytest.mark.fr("FR-035")
def test_nested_skills_dir_counts(kmaker, sink, tmp_path):
    work = tmp_path / "work"
    write_skill(work / "vendor" / "skills", "zeta", "Zeta.")
    _run(kmaker, [tc("read_file", {"path": "vendor/skills/zeta/SKILL.md"}, "z1"), "done"])
    [read] = ev(sink, "skill.read")
    assert (read["skill"], read["call_id"]) == ("zeta", "z1")


@pytest.mark.fr("FR-038")
@pytest.mark.parametrize("path", [
    "skills/alpha/notes.md", "skills/alpha/sub/SKILL.md", "skills/SKILL.md", "docs/SKILL.md",
    "other.txt",
])
def test_non_skill_files_do_not_emit(kmaker, sink, tmp_path, path):
    _ws(tmp_path)
    result, _ = _run(kmaker, [tc("read_file", {"path": path}, "n1"), "done"])
    [call] = ev(sink, "tool.call")
    assert call["ok"] is True
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_lowercase_skill_md_is_not_a_skill_file(kmaker, sink, tmp_path):
    work = tmp_path / "work"
    (work / "skills" / "lower").mkdir(parents=True)
    (work / "skills" / "lower" / "skill.md").write_text("lowercase")
    if (work / "skills" / "lower" / "SKILL.md").exists():
        pytest.skip("case-insensitive file system")
    _run(kmaker, [tc("read_file", {"path": "skills/lower/skill.md"}, "l1"), "done"])
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_missing_skill_file_does_not_emit(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [tc("read_file", {"path": "skills/ghost/SKILL.md"}, "f1"), "done"])
    [call] = ev(sink, "tool.call")
    assert call["ok"] is False
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_directory_named_skill_md_does_not_emit(kmaker, sink, tmp_path):
    work = _ws(tmp_path)
    # under a skills root the loader never lists (a directory there would break the loader)
    (work / "vendor" / "skills" / "odd" / "SKILL.md").mkdir(parents=True)
    _run(kmaker, [tc("read_file", {"path": "vendor/skills/odd/SKILL.md"}, "f2"), "done"])
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_too_large_skill_file_does_not_emit(kmaker, sink, tmp_path):
    work = _ws(tmp_path)
    big = write_skill(work / "vendor" / "skills", "huge") / "SKILL.md"
    with open(big, "r+b") as fh:
        fh.truncate(101 * 1024 * 1024)  # sparse: no real disk use
    _run(kmaker, [tc("read_file", {"path": "vendor/skills/huge/SKILL.md"}, "f3"), "done"])
    [call] = ev(sink, "tool.call")
    assert call["ok"] is False
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_unreadable_skill_file_does_not_emit(kmaker, sink, tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    work = tmp_path / "work"
    # A skill file by the Terms (grandparent named "skills") that the loader never lists,
    # so locking it cannot disturb the system prompt.
    locked = write_skill(work / "vendor" / "skills", "zeta", "Zeta.") / "SKILL.md"
    locked.chmod(0)
    try:
        _run(kmaker, [tc("read_file", {"path": "vendor/skills/zeta/SKILL.md"}, "f4"), "done"])
    finally:
        locked.chmod(0o600)
    calls_ = [e for e in ev(sink, "tool.call") if e["call_id"] == "f4"]
    assert calls_ and calls_[0]["ok"] is False
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_gate_denied_read_does_not_emit(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [tc("read_file", {"path": "skills/alpha/SKILL.md"}, "p1"), "done"],
         policy=DefaultPolicy(deny_rules=(("fs.read", "*alpha*"),)))
    assert ev(sink, "skill.read") == []
    assert [e for e in ev(sink, "tool.call") if e["call_id"] == "p1"] == []


@pytest.mark.fr("FR-038")
def test_summary_and_always_on_do_not_emit(kmaker, sink, tmp_path):
    work = _ws(tmp_path)
    write_skill(work / "skills", "always-h006", "Always on.", extra_front="always: true\n")
    result, _ = _run(kmaker, ["no tools used"])
    assert result.stop_reason == "completed"
    assert ev(sink, "skill.read") == []


# -- grep -----------------------------------------------------------------------------------


@pytest.mark.fr("FR-035", "FR-036", "FR-037", "SC-006")
def test_us5_grep_content_one_event_per_skill_file(kmaker, sink, tmp_path):
    """US5 scenario 1 and SC-006 (k = 2)."""
    work = _ws(tmp_path)
    result, _ = _run(kmaker, [tc("grep", {"pattern": "NEEDLE", "path": "skills",
                                          "output_mode": "content"}, "g1"), "done"])
    [call] = ev(sink, "tool.call")
    assert call["ok"] is True and call["call_id"] == "g1"
    reads = ev(sink, "skill.read")
    assert len(reads) == 2
    assert sorted(e["skill"] for e in reads) == ["alpha", "beta"]
    assert all(e["via"] == "grep" and e["call_id"] == call["call_id"] for e in reads)
    assert {e["path"] for e in reads} == {
        _resolved(work / "skills" / "alpha" / "SKILL.md"),
        _resolved(work / "skills" / "beta" / "SKILL.md"),
    }
    assert all(set(strip(e)) == SKILL_READ_KEYS for e in reads)
    _assert_before_tool_call(sink, "g1")


@pytest.mark.fr("FR-035", "SC-006")
def test_grep_default_mode_is_content(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [tc("grep", {"pattern": "NEEDLE", "path": "skills"}, "g2"), "done"])
    reads = ev(sink, "skill.read")
    assert sorted(e["skill"] for e in reads) == ["alpha", "beta"]


@pytest.mark.fr("FR-035", "FR-037", "SC-006")
def test_grep_k_skill_files(kmaker, sink, tmp_path):
    work = tmp_path / "work"
    k = 5
    for i in range(k):
        write_skill(work / "skills", f"s{i}", f"line TOKEN{i} here\nmore TOKEN again")
    write_skill(work / "skills", "nomatch", "nothing")
    _run(kmaker, [tc("grep", {"pattern": "TOKEN", "path": "skills",
                              "output_mode": "content"}, "gk"), "done"])
    reads = ev(sink, "skill.read")
    assert len(reads) == k
    assert sorted(e["skill"] for e in reads) == [f"s{i}" for i in range(k)]
    assert {e["call_id"] for e in reads} == {"gk"}


@pytest.mark.fr("FR-035")
def test_grep_head_limit_only_returned_files(kmaker, sink, tmp_path):
    _ws(tmp_path)
    result, _ = _run(kmaker, [tc("grep", {"pattern": "(alpha line one|beta) NEEDLE",
                                          "path": "skills",
                                          "output_mode": "content", "head_limit": 1,
                                          "context_before": 0, "context_after": 0}, "gh"),
                              "done"])
    content = "".join(
        str(m.get("content")) for m in result.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "gh"
    )
    returned = {n for n in ("alpha", "beta") if f"{n}/SKILL.md" in content}
    assert len(returned) == 1, content[:500]
    reads = ev(sink, "skill.read")
    assert {e["skill"] for e in reads} == returned and len(reads) == 1


@pytest.mark.fr("FR-038")
@pytest.mark.parametrize("mode", ["files_with_matches", "count"])
def test_us5_grep_other_modes_do_not_emit(kmaker, sink, tmp_path, mode):
    """US5 scenario 2."""
    _ws(tmp_path)
    _run(kmaker, [tc("grep", {"pattern": "NEEDLE", "path": "skills", "output_mode": mode},
                     "g3"), "done"])
    [call] = ev(sink, "tool.call")
    assert call["ok"] is True
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-038")
def test_grep_no_match_and_non_skill_matches(kmaker, sink, tmp_path):
    _ws(tmp_path)
    _run(kmaker, [calls(("grep", {"pattern": "ZZZ-no-match", "path": "skills"}, "g4"),
                        ("grep", {"pattern": "plain file|outside a skills root|nested NEEDLE",
                                  "path": "."}, "g5")), "done"])
    assert ev(sink, "skill.read") == []


# -- FR-039 / FR-040 --------------------------------------------------------------------------


@pytest.mark.fr("FR-039")
def test_exec_reads_are_not_inferred(kmaker, sink, tmp_path):
    """006-Q2 default (no): shell reads of a SKILL.md stay invisible."""
    _ws(tmp_path)
    _run(kmaker, [tc("exec", {"command": "cat skills/alpha/SKILL.md"}, "x1"), "done"])
    assert ev(sink, "skill.read") == []


@pytest.mark.fr("FR-039", "FR-040")
def test_events_catalogue_text():
    from moeka.trace import EVENTS

    text = EVENTS["skill.read"]
    assert "via" in text and "call_id" in text
    assert "lower bound" in text.lower()
