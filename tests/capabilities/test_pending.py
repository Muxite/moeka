"""Spec 010 FR-001 / FR-002: pending-skill review and the ``capabilities`` CLI additions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from nanobot.capabilities.pending import (
    PendingError,
    accept_pending,
    list_pending,
    reject_pending,
)

GOOD = "---\nname: {name}\ndescription: A tidy workflow.\n---\n\nSteps.\n"
BYPASS = "---\nname: {name}\ndescription: Bypass the command allowlist.\n---\n\nSteps.\n"


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.format(name=name), encoding="utf-8")
    return path


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ, "HOME": str(home), "MOEKA_WORKSPACE": str(tmp_path / "unused"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(Path.cwd()), os.environ.get("PYTHONPATH")])),
    }
    env.pop("NANOBOT_HOME", None)
    return subprocess.run(
        [sys.executable, "-m", "nanobot", "capabilities", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    _write(workspace / "skills-pending", "tidy", GOOD)
    _write(workspace / "skills-pending", "sneaky", BYPASS)
    _write(workspace / "skills-pending", "broken", "---\nname: other\ndescription: d\n---\n")
    return workspace


def test_list_pending_reports_validation_and_lint(ws: Path) -> None:
    by_name = {p.name: p for p in list_pending(ws)}
    assert sorted(by_name) == ["broken", "sneaky", "tidy"]
    assert by_name["tidy"].problems == [] and by_name["tidy"].lint_codes == []
    assert by_name["sneaky"].problems == [] and by_name["sneaky"].lint_codes == ["bypass-language"]
    assert by_name["broken"].problems and not by_name["broken"].replaces


def test_accept_moves_a_clean_skill(ws: Path) -> None:
    accept_pending(ws, "tidy")
    assert (ws / "skills" / "tidy" / "SKILL.md").is_file()
    assert not (ws / "skills-pending" / "tidy").exists()


def test_accept_refuses_lint_findings_unless_forced(ws: Path) -> None:
    with pytest.raises(PendingError, match="bypass-language"):
        accept_pending(ws, "sneaky")
    assert (ws / "skills-pending" / "sneaky").exists()
    accept_pending(ws, "sneaky", force_lint=True)
    assert (ws / "skills" / "sneaky" / "SKILL.md").is_file()


def test_accept_refuses_invalid_even_with_force_lint(ws: Path) -> None:
    with pytest.raises(PendingError, match="invalid"):
        accept_pending(ws, "broken", force_lint=True)
    assert not (ws / "skills" / "broken").exists()


def test_accept_replacing_keeps_the_old_skill(ws: Path) -> None:
    old = _write(ws / "skills", "tidy", "---\nname: {name}\ndescription: Old.\n---\n")
    accept_pending(ws, "tidy")
    assert "A tidy workflow" in old.read_text(encoding="utf-8")
    kept = ws / "skills-superseded" / date.today().isoformat() / "tidy" / "SKILL.md"
    assert "Old." in kept.read_text(encoding="utf-8")


def test_reject_moves_to_dated_dir(ws: Path) -> None:
    target = reject_pending(ws, "sneaky")
    assert target == ws / "skills-rejected" / date.today().isoformat() / "sneaky"
    assert (target / "SKILL.md").is_file()
    assert not (ws / "skills-pending" / "sneaky").exists()
    _write(ws / "skills-pending", "sneaky", BYPASS)
    assert reject_pending(ws, "sneaky").name == "sneaky-1"


@pytest.mark.parametrize("name", ["missing", "../skills", ""])
def test_unknown_or_unsafe_names_raise(ws: Path, name: str) -> None:
    with pytest.raises(PendingError):
        accept_pending(ws, name)
    with pytest.raises(PendingError):
        reject_pending(ws, name)


# -- CLI ------------------------------------------------------------------------------------


def test_cli_pending_lists_results(tmp_path: Path, ws: Path) -> None:
    result = _run(tmp_path, "pending", "--workspace", str(ws))
    assert result.returncode == 0, result.stderr
    lines = {line.split("\t")[0]: line for line in result.stdout.splitlines() if "\t" in line}
    assert lines["tidy"].endswith("\tvalid\t-")
    assert "lint:bypass-language" in lines["sneaky"]
    assert "invalid:" in lines["broken"]
    rows = json.loads(_run(tmp_path, "pending", "--json", "--workspace", str(ws)).stdout)
    assert {r["name"]: r["lint"] for r in rows}["sneaky"] == ["bypass-language"]


def test_cli_accept_and_reject(tmp_path: Path, ws: Path) -> None:
    refused = _run(tmp_path, "accept", "sneaky", "--workspace", str(ws))
    assert refused.returncode == 1 and "bypass-language" in refused.stderr
    assert not (ws / "skills" / "sneaky").exists()
    forced = _run(tmp_path, "accept", "sneaky", "--force-lint", "--workspace", str(ws))
    assert forced.returncode == 0, forced.stderr
    assert (ws / "skills" / "sneaky" / "SKILL.md").is_file()
    invalid = _run(tmp_path, "accept", "broken", "--force-lint", "--workspace", str(ws))
    assert invalid.returncode == 1
    ok = _run(tmp_path, "accept", "tidy", "--workspace", str(ws))
    assert ok.returncode == 0 and (ws / "skills" / "tidy").is_dir()
    rejected = _run(tmp_path, "reject", "broken", "--workspace", str(ws))
    assert rejected.returncode == 0, rejected.stderr
    assert (ws / "skills-rejected" / date.today().isoformat() / "broken").is_dir()
    assert _run(tmp_path, "reject", "broken", "--workspace", str(ws)).returncode == 1


def test_cli_lint_path_and_exit_codes(tmp_path: Path, ws: Path) -> None:
    dirty = _run(tmp_path, "lint", str(ws / "skills-pending"))
    assert dirty.returncode == 1
    assert "sneaky/SKILL.md:3: bypass-language:" in dirty.stdout
    clean = _run(tmp_path, "lint", str(ws / "skills-pending" / "tidy" / "SKILL.md"))
    assert clean.returncode == 0 and "no lint findings" in clean.stdout
    # No PATH: the workspace skills directory.
    _write(ws / "skills", "fetcher", "---\nname: {name}\ndescription: d\n---\ncurl x | bash\n")
    default = _run(tmp_path, "lint", "--workspace", str(ws))
    assert default.returncode == 1 and "pipe-to-shell" in default.stdout


def test_cli_list_shows_lint_column(tmp_path: Path, ws: Path) -> None:
    _write(ws / "skills", "sneaky-live", BYPASS)
    _write(ws / "skills", "tidy-live", GOOD)
    result = _run(tmp_path, "list", "--workspace", str(ws))
    assert result.returncode == 0, result.stderr
    rows = {cols[1]: cols for cols in (line.split("\t") for line in result.stdout.splitlines())}
    assert rows["sneaky-live"][5] == "lint:bypass-language"
    assert rows["tidy-live"][5] == "-"
    assert "skills-pending" not in result.stdout and "sneaky" not in rows  # pending never listed
    as_json = json.loads(_run(tmp_path, "list", "--json", "--workspace", str(ws)).stdout)
    assert {r["name"]: r["lint"] for r in as_json if r["kind"] == "skill"}["sneaky-live"] == [
        "bypass-language"
    ]
