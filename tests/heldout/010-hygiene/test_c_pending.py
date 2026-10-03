"""Part C: pending skills (FR-001, SC-002): Dream write mapping, the loader, and
``nanobot capabilities pending|accept|reject``."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest
from _h010 import (
    CLEAN_BODY,
    LINTY_BODY,
    cli,
    config,
    entries,
    out,
    repo_root,
    request_text,
    run_dream,
    skill_names,
    tool_call,
    tree,
    write_skill,
)

REPO = repo_root()
NEW = "weekly-digest"
NEW_TEXT = (
    "---\nname: weekly-digest\ndescription: Digest the weekly report into action items.\n---\n\n"
    "# weekly-digest\n\nRead the report, list action items, send them to the team.\n"
)
EXISTING = "report-helper"
MERGED_TEXT = (
    "---\nname: report-helper\ndescription: Help with reports (merged by Dream).\n---\n\n"
    "# report-helper\n\nMERGED-BODY: also handle the monthly report.\n"
)
EXISTING_MARKER = "EXISTING-SKILL-BODY-MARKER"


def _seed_existing(work):
    write_skill(work / "skills", EXISTING, "Help with reports",
                body=f"# {{name}}\n\n{EXISTING_MARKER}: use when needed.\n")
    return tree(work / "skills")


def _write_new(path=f"skills/{NEW}/SKILL.md"):
    return [tool_call("write_file", {"path": path, "content": NEW_TEXT})]


# -- config ------------------------------------------------------------------------------------


@pytest.mark.fr("FR-001")
def test_skill_writes_default_is_pending():
    from nanobot.config.schema import Config, DreamConfig

    assert DreamConfig().skill_writes == "pending"
    assert Config().agents.defaults.dream.skill_writes == "pending"


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("key", ["skill_writes", "skillWrites"])
@pytest.mark.parametrize("mode", ["pending", "direct"])
def test_skill_writes_accepts_both_modes_and_spellings(key, mode):
    cfg = config({"agents": {"defaults": {"dream": {key: mode}}}})
    assert cfg.agents.defaults.dream.skill_writes == mode


@pytest.mark.fr("FR-001")
def test_skill_writes_rejects_unknown_value():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        config({"agents": {"defaults": {"dream": {"skillWrites": "later"}}}})


# -- Dream: pending mode -------------------------------------------------------------------------


@pytest.mark.fr("SC-002", "FR-001")
async def test_sc002_default_config_dream_create_goes_to_pending(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    _, result, calls = await run_dream(work, _write_new(), config=config())
    assert calls, "Dream must reach the provider"
    assert result.status == "completed", result
    assert tree(work / "skills") == before
    assert entries(work / "skills-pending") == [NEW]
    assert (work / "skills-pending" / NEW / "SKILL.md").read_text(encoding="utf-8") == NEW_TEXT


@pytest.mark.fr("FR-001", "SC-002")
async def test_bare_loop_default_is_pending(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    _, result, _ = await run_dream(work, _write_new())
    assert tree(work / "skills") == before
    assert entries(work / "skills-pending") == [NEW]
    assert (work / "skills-pending" / NEW / "SKILL.md").read_text(encoding="utf-8") == NEW_TEXT


@pytest.mark.fr("FR-001")
async def test_pending_mode_apply_patch_add_goes_to_pending(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    patch = tool_call("apply_patch", {"edits": [
        {"path": f"skills/{NEW}/SKILL.md", "action": "add", "new_text": NEW_TEXT},
    ]})
    await run_dream(work, [patch], config=config())
    assert tree(work / "skills") == before
    assert entries(work / "skills-pending") == [NEW]
    assert "weekly-digest" in (work / "skills-pending" / NEW / "SKILL.md").read_text(
        encoding="utf-8")


@pytest.mark.fr("FR-001")
async def test_pending_mode_direct_write_into_skills_pending_is_allowed(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    await run_dream(work, _write_new(f"skills-pending/{NEW}/SKILL.md"), config=config())
    assert tree(work / "skills") == before
    assert (work / "skills-pending" / NEW / "SKILL.md").read_text(encoding="utf-8") == NEW_TEXT


@pytest.mark.fr("FR-001")
async def test_pending_mode_merge_rewrite_goes_to_pending(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    merge = tool_call("write_file", {"path": f"skills/{EXISTING}/SKILL.md",
                                     "content": MERGED_TEXT})
    await run_dream(work, [merge], config=config())
    assert tree(work / "skills") == before
    assert entries(work / "skills-pending") == [EXISTING]
    assert (work / "skills-pending" / EXISTING / "SKILL.md").read_text(
        encoding="utf-8") == MERGED_TEXT


@pytest.mark.fr("FR-001")
async def test_pending_mode_merge_edit_goes_to_pending(tmp_path):
    work = tmp_path / "work"
    before = _seed_existing(work)
    edit = tool_call("edit_file", {
        "path": f"skills/{EXISTING}/SKILL.md",
        "old_text": f"{EXISTING_MARKER}: use when needed.",
        "new_text": f"{EXISTING_MARKER}: use when needed, and on Fridays.",
    })
    await run_dream(work, [edit], config=config())
    assert tree(work / "skills") == before
    assert entries(work / "skills-pending") == [EXISTING]
    text = (work / "skills-pending" / EXISTING / "SKILL.md").read_text(encoding="utf-8")
    assert "and on Fridays." in text
    assert "name: report-helper" in text


@pytest.mark.fr("FR-001")
async def test_pending_mode_dream_can_still_read_skills(tmp_path):
    work = tmp_path / "work"
    _seed_existing(work)
    read = tool_call("read_file", {"path": f"skills/{EXISTING}/SKILL.md"})
    _, _, calls = await run_dream(work, [read], config=config())
    assert len(calls) >= 2
    tool_msgs = [m for m in calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and EXISTING_MARKER in str(tool_msgs[-1].get("content"))


@pytest.mark.fr("FR-001")
async def test_pending_mode_memory_edits_are_unaffected(tmp_path):
    work = tmp_path / "work"
    _seed_existing(work)
    edit = tool_call("edit_file", {"path": "memory/MEMORY.md", "old_text": "Project X active",
                                   "new_text": "Project Y active"})
    loop, _, _ = await run_dream(work, [edit], config=config())
    assert "Project Y active" in loop.context.memory.memory_file.read_text(encoding="utf-8")
    assert entries(work / "skills-pending") == []


@pytest.mark.fr("FR-001")
async def test_pending_mode_prompt_mentions_skills_pending(tmp_path):
    work = tmp_path / "work"
    _, _, calls = await run_dream(work, [], config=config())
    assert calls
    assert "skills-pending" in request_text(calls[0])


@pytest.mark.fr("FR-001")
def test_dream_template_mentions_skills_pending():
    text = (REPO / "nanobot" / "templates" / "agent" / "dream.md").read_text(encoding="utf-8")
    assert "skills-pending" in text


# -- Dream: direct mode --------------------------------------------------------------------------


@pytest.mark.fr("FR-001")
async def test_direct_mode_writes_to_skills(tmp_path):
    work = tmp_path / "work"
    _seed_existing(work)
    cfg = config({"agents": {"defaults": {"dream": {"skillWrites": "direct"}}}})
    _, result, _ = await run_dream(work, _write_new(), config=cfg)
    assert result.status == "completed", result
    assert (work / "skills" / NEW / "SKILL.md").read_text(encoding="utf-8") == NEW_TEXT
    assert entries(work / "skills-pending") == []


@pytest.mark.fr("FR-001")
async def test_direct_mode_merge_rewrites_in_place(tmp_path):
    work = tmp_path / "work"
    _seed_existing(work)
    cfg = config({"agents": {"defaults": {"dream": {"skill_writes": "direct"}}}})
    merge = tool_call("write_file", {"path": f"skills/{EXISTING}/SKILL.md",
                                     "content": MERGED_TEXT})
    await run_dream(work, [merge], config=cfg)
    assert (work / "skills" / EXISTING / "SKILL.md").read_text(encoding="utf-8") == MERGED_TEXT
    assert entries(work / "skills-pending") == []


# -- loader --------------------------------------------------------------------------------------


@pytest.mark.fr("FR-001")
def test_loader_never_lists_or_loads_pending(tmp_path):
    from nanobot.agent.skills import SkillsLoader

    ws = tmp_path / "ws"
    write_skill(ws / "skills", "ws-live", "A live workspace skill")
    write_skill(ws / "skills-pending", "ws-waiting", "A pending skill")
    write_skill(ws / "skills-rejected" / "2026-10-01", "ws-gone", "A rejected skill")
    loader = SkillsLoader(ws, builtin_skills_dir=tmp_path / "no-builtins")
    names = skill_names(loader)
    assert "ws-live" in names
    assert "ws-waiting" not in names and "ws-gone" not in names
    assert loader.load_skill("ws-waiting") is None
    assert "ws-waiting" not in loader.build_skills_summary()
    assert loader.get_explicitly_invoked_skills("please run $ws-waiting now") == []


@pytest.mark.fr("FR-001")
def test_capabilities_list_does_not_show_pending(tmp_path):
    ws = tmp_path / "ws"
    write_skill(ws / "skills", "ws-live", "A live workspace skill")
    write_skill(ws / "skills-pending", "ws-waiting", "A pending skill")
    proc = cli(tmp_path, "list", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert "ws-live" in proc.stdout
    assert "ws-waiting" not in proc.stdout


# -- CLI: pending ----------------------------------------------------------------------------------


def _pending_ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    write_skill(ws / "skills-pending", "pend-clean", "Digest reports", body=CLEAN_BODY)
    write_skill(ws / "skills-pending", "pend-linty", "Runs blocked commands", body=LINTY_BODY)
    write_skill(ws / "skills-pending", "pend-invalid", "Bad identity", front_name="other-name")
    return ws


def _lines(text, name):
    return [ln for ln in text.splitlines() if name in ln]


@pytest.mark.fr("FR-001")
def test_pending_lists_each_with_validation_and_lint(tmp_path):
    from nanobot.capabilities.manifest import validate_skill

    ws = _pending_ws(tmp_path)
    proc = cli(tmp_path, "pending", "--workspace", str(ws))
    assert proc.returncode in (0, 1), out(proc)[-3000:]
    text = out(proc)
    for name in ("pend-clean", "pend-linty", "pend-invalid"):
        assert name in text, text
    linty_block = text[text.index("pend-linty"):]
    assert "bypass-language" in linty_block and "secret-path" in text
    clean_lines = _lines(text, "pend-clean")
    assert not any(code in ln for ln in clean_lines
                   for code in ("bypass-language", "secret-path"))
    problems = validate_skill(ws / "skills-pending" / "pend-invalid" / "SKILL.md")
    assert problems
    assert problems[0] in text or re.search(r"invalid", text[text.index("pend-invalid"):],
                                            re.IGNORECASE), text


@pytest.mark.fr("FR-001")
def test_pending_with_none_is_not_an_error(tmp_path):
    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    proc = cli(tmp_path, "pending", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]


# -- CLI: accept -----------------------------------------------------------------------------------


@pytest.mark.fr("FR-001")
def test_accept_valid_clean_moves_into_skills(tmp_path):
    ws = _pending_ws(tmp_path)
    original = (ws / "skills-pending" / "pend-clean" / "SKILL.md").read_bytes()
    proc = cli(tmp_path, "accept", "pend-clean", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert (ws / "skills" / "pend-clean" / "SKILL.md").read_bytes() == original
    assert not (ws / "skills-pending" / "pend-clean").exists()
    assert "pend-linty" in entries(ws / "skills-pending")


@pytest.mark.fr("FR-001")
def test_accepted_skill_is_listed_by_the_loader(tmp_path):
    from nanobot.agent.skills import SkillsLoader

    ws = _pending_ws(tmp_path)
    assert "pend-clean" not in skill_names(SkillsLoader(ws, builtin_skills_dir=tmp_path / "nb"))
    proc = cli(tmp_path, "accept", "pend-clean", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert "pend-clean" in skill_names(SkillsLoader(ws, builtin_skills_dir=tmp_path / "nb"))


@pytest.mark.fr("FR-001")
def test_accept_with_lint_findings_exits_one_and_stays_pending(tmp_path):
    ws = _pending_ws(tmp_path)
    proc = cli(tmp_path, "accept", "pend-linty", "--workspace", str(ws))
    assert proc.returncode == 1, out(proc)[-3000:]
    assert (ws / "skills-pending" / "pend-linty" / "SKILL.md").is_file()
    assert not (ws / "skills" / "pend-linty").exists()
    assert "bypass-language" in out(proc) or "lint" in out(proc).lower()


@pytest.mark.fr("FR-001")
def test_accept_force_lint_moves_linty_skill(tmp_path):
    ws = _pending_ws(tmp_path)
    original = (ws / "skills-pending" / "pend-linty" / "SKILL.md").read_bytes()
    proc = cli(tmp_path, "accept", "pend-linty", "--force-lint", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert (ws / "skills" / "pend-linty" / "SKILL.md").read_bytes() == original
    assert not (ws / "skills-pending" / "pend-linty").exists()


@pytest.mark.fr("FR-001")
def test_accept_invalid_exits_one_and_stays_pending(tmp_path):
    ws = _pending_ws(tmp_path)
    proc = cli(tmp_path, "accept", "pend-invalid", "--workspace", str(ws))
    assert proc.returncode == 1, out(proc)[-3000:]
    assert (ws / "skills-pending" / "pend-invalid" / "SKILL.md").is_file()
    assert not (ws / "skills" / "pend-invalid").exists()


@pytest.mark.fr("FR-001")
def test_accept_invalid_with_force_lint_still_exits_one(tmp_path):
    ws = _pending_ws(tmp_path)
    proc = cli(tmp_path, "accept", "pend-invalid", "--force-lint", "--workspace", str(ws))
    assert proc.returncode == 1, out(proc)[-3000:]
    assert (ws / "skills-pending" / "pend-invalid" / "SKILL.md").is_file()
    assert not (ws / "skills" / "pend-invalid").exists()


@pytest.mark.fr("FR-001")
def test_accept_unknown_name_fails(tmp_path):
    ws = _pending_ws(tmp_path)
    before = tree(ws)
    proc = cli(tmp_path, "accept", "no-such-skill", "--workspace", str(ws))
    assert proc.returncode != 0, out(proc)[-3000:]
    assert "No such command" not in out(proc)
    assert tree(ws) == before


# -- CLI: reject -----------------------------------------------------------------------------------


def _date_tokens():
    days = {date.today(), datetime.now(timezone.utc).date()}
    days |= {d - timedelta(days=1) for d in list(days)}  # run straddling midnight
    return {d.isoformat() for d in days} | {d.strftime("%Y%m%d") for d in days}


@pytest.mark.fr("FR-001")
def test_reject_moves_to_dated_rejected_dir(tmp_path):
    ws = _pending_ws(tmp_path)
    original = (ws / "skills-pending" / "pend-linty" / "SKILL.md").read_bytes()
    proc = cli(tmp_path, "reject", "pend-linty", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert not (ws / "skills-pending" / "pend-linty").exists()
    assert not (ws / "skills" / "pend-linty").exists()
    moved = [p for p in (ws / "skills-rejected").rglob("SKILL.md") if p.read_bytes() == original]
    assert len(moved) == 1, list((ws / "skills-rejected").rglob("*"))
    rel = moved[0].relative_to(ws / "skills-rejected")
    first = rel.parts[0]
    assert any(tok in first for tok in _date_tokens()), rel
    assert "pend-linty" in rel.parts[1:-1], rel


@pytest.mark.fr("FR-001")
def test_reject_does_not_touch_other_pending(tmp_path):
    ws = _pending_ws(tmp_path)
    proc = cli(tmp_path, "reject", "pend-clean", "--workspace", str(ws))
    assert proc.returncode == 0, out(proc)[-3000:]
    assert entries(ws / "skills-pending") == ["pend-invalid", "pend-linty"]
    assert entries(ws / "skills") == []


@pytest.mark.fr("FR-001")
def test_reject_unknown_name_fails(tmp_path):
    ws = _pending_ws(tmp_path)
    before = tree(ws)
    proc = cli(tmp_path, "reject", "no-such-skill", "--workspace", str(ws))
    assert proc.returncode != 0, out(proc)[-3000:]
    assert "No such command" not in out(proc)
    assert tree(ws) == before
