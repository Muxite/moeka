"""``nanobot capabilities list|explain|validate`` (spec 011 FR-013)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _skill(workspace: Path, name: str, front: str) -> None:
    path = workspace / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\nBody.\n", encoding="utf-8")


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
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    _skill(ws, "pdf-notes", "name: pdf-notes\ndescription: Summarise a PDF into notes.\n"
           "io:\n  inputs: [file:pdf]\n  outputs: [text/markdown]\n  effects: [read-workspace]\n")
    _skill(ws, "repo-tour", "name: repo-tour\ndescription: Tour a git repository.\n"
           "io:\n  inputs: [url/git]\n")
    _skill(ws, "plain", "name: plain\ndescription: An untyped skill.\n")
    return ws


def test_list_json_shows_every_capability(tmp_path, workspace):
    result = _run(tmp_path, "list", "--json", "--workspace", str(workspace))
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    by_key = {(row["kind"], row["name"]): row for row in rows}
    pdf = by_key[("skill", "pdf-notes")]
    assert pdf["io"]["inputs"]["all"] == ["file:pdf"]
    assert pdf["available"] is True and len(pdf["content_sha256"]) == 12
    assert by_key[("skill", "plain")]["io"] == "untyped"
    assert ("tool", "exec") in by_key
    assert "exec" in by_key[("tool", "exec")]["io"]["effects"]
    assert ("skill", "weather") in by_key  # built-ins are listed too


def test_list_text(tmp_path, workspace):
    result = _run(tmp_path, "list", "--workspace", str(workspace))
    assert result.returncode == 0, result.stderr
    lines = [line.split("\t") for line in result.stdout.splitlines()]
    plain = next(line for line in lines if line[1] == "plain")
    assert plain[0] == "skill" and plain[2] == "untyped" and len(plain[4]) == 12


def test_explain_shows_typing_survivors_and_selection(tmp_path, workspace):
    attachment = tmp_path / "report.pdf"
    result = _run(tmp_path, "explain", "summarise this as markdown", "--attach", str(attachment),
                  "--k", "2", "--json", "--workspace", str(workspace))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert "file:pdf" in report["task_types"] and "text/query" in report["task_types"]
    assert report["requested_output"] == "text/markdown"
    names = [row["name"] for row in report["survivors"]]
    assert "pdf-notes" in names and "repo-tour" not in names
    assert all(isinstance(row["score"], float) for row in report["survivors"])
    assert len(report["selected"]) == 2 and report["fallback"] is None
    assert not attachment.exists()  # --attach names a file; it is never read

    text = _run(tmp_path, "explain", "summarise this as markdown", "--attach", str(attachment),
                "--workspace", str(workspace))
    assert text.returncode == 0, text.stderr
    for label in ("task types:", "requested output: text/markdown", "survivors (", "selected (",
                  "fallback:"):
        assert label in text.stdout


def test_validate_passes_for_valid_workspace(tmp_path, workspace):
    result = _run(tmp_path, "validate", "--workspace", str(workspace))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 workspace skill(s) valid" in result.stdout


def test_validate_lists_each_invalid_skill_and_exits_1(tmp_path, workspace):
    _skill(workspace, "bad-name", "name: other\ndescription: d\n")
    _skill(workspace, "bad-io", "name: bad-io\ndescription: d\nio:\n  effects: [teleport]\n")
    result = _run(tmp_path, "validate", "--workspace", str(workspace))
    assert result.returncode == 1
    assert "INVALID bad-name" in result.stdout and "INVALID bad-io" in result.stdout
    assert "teleport" in result.stdout
    assert "2 of 5 workspace skill(s) invalid" in result.stdout
