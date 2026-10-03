"""Part E: library operations: the CLI (FR-013) and built-in annotations (FR-014)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from _h011 import cap, repo_root, write_skill

REPO = repo_root()


def _cli(tmp_path, *args, timeout=180):
    home = tmp_path / "clihome"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("MOEKA_WORKSPACE", "MOEKA_CONFIG", "MOEKA_STATE", "NANOBOT_HOME")}
    env.update(HOME=str(home), PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1",
               NO_COLOR="1", COLUMNS="250", TERM="dumb")
    return subprocess.run([sys.executable, "-m", "nanobot", "capabilities", *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=timeout)


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    typed = write_skill(ws / "skills", "ws-pdf-digest", "Digest PDF reports as markdown",
                        io={"inputs": ["file:pdf"], "outputs": ["text/markdown"],
                            "effects": ["read-workspace"]})
    untyped = write_skill(ws / "skills", "ws-plain", "A plain untyped workspace skill")
    return ws, {"ws-pdf-digest": typed, "ws-plain": untyped}


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _records(payload):
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return value
        raise AssertionError(f"no list in {payload!r}")
    assert isinstance(payload, list)
    return payload


# -- list ---------------------------------------------------------------------------------------


@pytest.mark.fr("FR-013")
def test_list_plain(tmp_path):
    ws, skills = _workspace(tmp_path)
    proc = _cli(tmp_path, "list", "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = proc.stdout
    for name, path in skills.items():
        assert name in out
        assert _sha12(path) in out, name
    assert "untyped" in out
    assert "file:pdf" in out
    assert "skill" in out and "tool" in out
    line = next(ln for ln in out.splitlines() if "ws-plain" in ln)
    assert "untyped" in line


@pytest.mark.fr("FR-013")
def test_list_json(tmp_path):
    ws, skills = _workspace(tmp_path)
    proc = _cli(tmp_path, "list", "--json", "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    records = _records(json.loads(proc.stdout))
    by_name = {(r["kind"], r["name"]): r for r in records}
    for name, path in skills.items():
        rec = by_name[("skill", name)]
        assert {"kind", "name", "io", "available"} <= set(rec)
        assert isinstance(rec["available"], bool)
        assert _sha12(path) in json.dumps(rec)
    assert by_name[("skill", "ws-plain")]["io"] in (None, "untyped")
    typed_io = json.dumps(by_name[("skill", "ws-pdf-digest")]["io"])
    assert "file:pdf" in typed_io and "text/markdown" in typed_io
    assert any(kind == "tool" for kind, _ in by_name), "tools are capabilities too"
    assert ("tool", "read_file") in by_name
    assert any(kind == "skill" and r.get("available") is not None
               for (kind, _), r in by_name.items())


# -- explain ------------------------------------------------------------------------------------


@pytest.mark.fr("FR-013")
def test_explain_text_message(tmp_path):
    ws, _ = _workspace(tmp_path)
    proc = _cli(tmp_path, "explain", "Please summarise ~/docs/report.pdf as markdown",
                "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = proc.stdout
    for needle in ("file:pdf", "path", "text/markdown", "ws-pdf-digest"):
        assert needle in out, needle
    assert "fallback" in out.lower()


@pytest.mark.fr("FR-013")
def test_explain_with_attachment(tmp_path):
    ws, _ = _workspace(tmp_path)
    attach = tmp_path / "table.csv"
    attach.write_text("a,b\n1,2\n")
    proc = _cli(tmp_path, "explain", "show me some stats", "--attach", str(attach),
                "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "data/csv" in proc.stdout


@pytest.mark.fr("FR-013", "FR-009")
def test_explain_k_and_small_fallback(tmp_path):
    ws, _ = _workspace(tmp_path)
    big = _cli(tmp_path, "explain", "hello", "--k", "64", "--workspace", str(ws))
    assert big.returncode == 0, big.stderr[-3000:]
    assert "small" in big.stdout
    small = _cli(tmp_path, "explain", "hello", "--k", "1", "--workspace", str(ws))
    assert small.returncode == 0, small.stderr[-3000:]


@pytest.mark.fr("FR-013")
def test_explain_uses_the_given_workspace(tmp_path):
    ws, _ = _workspace(tmp_path)
    other = tmp_path / "other"
    write_skill(other / "skills", "other-only-skill", "Digest PDF reports as markdown too",
                io={"inputs": ["file:pdf"], "outputs": ["text/markdown"], "effects": ["none"]})
    proc = _cli(tmp_path, "explain", "summarise ~/r.pdf as markdown", "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "other-only-skill" not in proc.stdout
    proc2 = _cli(tmp_path, "explain", "summarise ~/r.pdf as markdown", "--workspace", str(other))
    assert proc2.returncode == 0, proc2.stderr[-3000:]
    assert "other-only-skill" in proc2.stdout


# -- validate -----------------------------------------------------------------------------------


@pytest.mark.fr("FR-013", "FR-003")
def test_validate_all_valid_exits_zero(tmp_path):
    ws, _ = _workspace(tmp_path)
    proc = _cli(tmp_path, "validate", "--workspace", str(ws))
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


@pytest.mark.fr("FR-013", "FR-003")
def test_validate_invalid_exits_one_and_lists_each(tmp_path):
    ws, _ = _workspace(tmp_path)
    write_skill(ws / "skills", "broken-io", "bad io", io={"inputs": ["file:nope"]})
    write_skill(ws / "skills", "broken-name", "mismatch", front_name="something-else")
    proc = _cli(tmp_path, "validate", "--workspace", str(ws))
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = proc.stdout + proc.stderr
    assert "broken-io" in out and "broken-name" in out


@pytest.mark.fr("FR-013")
def test_cli_makes_no_model_call_without_credentials(tmp_path):
    """No provider key is configured anywhere; explain must still succeed (no model call)."""
    ws, _ = _workspace(tmp_path)
    proc = _cli(tmp_path, "explain", "review https://github.com/a/b as json",
                "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "url/git" in proc.stdout and "data/json" in proc.stdout


# -- FR-014 built-in annotations ----------------------------------------------------------------


def _bundled_skills():
    root = REPO / "nanobot" / "skills"
    return sorted(p for p in root.glob("*/SKILL.md"))


def _front(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    return yaml.safe_load(text.split("---", 2)[1])


@pytest.mark.fr("FR-014")
def test_bundled_skills_exist():
    assert len(_bundled_skills()) >= 10


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("skill", [p.parent.name for p in
                                   sorted((repo_root() / "nanobot" / "skills").glob("*/SKILL.md"))])
def test_every_bundled_skill_has_valid_io(skill):
    path = REPO / "nanobot" / "skills" / skill / "SKILL.md"
    meta = _front(path)
    assert isinstance(meta, dict) and "io" in meta, f"{skill} has no io block"
    spec = cap("manifest").parse_io(meta["io"])
    assert spec is not None
    assert cap("manifest").validate_skill(path) == []


def _tool_effects(module, cls_name):
    import importlib

    tool_cls = getattr(importlib.import_module(module), cls_name)
    raw = getattr(tool_cls, "io", None)
    assert raw is not None, f"{cls_name}.io is None"
    return set(cap("manifest").parse_io(raw).effects)


@pytest.mark.fr("FR-014", "FR-002")
def test_tool_base_io_defaults_to_none():
    from nanobot.agent.tools.base import Tool

    assert "io" in vars(Tool) or hasattr(Tool, "io")
    assert Tool.io is None


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("module, cls_name, required", [
    ("nanobot.agent.tools.shell", "ExecTool", {"exec"}),
    ("nanobot.agent.tools.exec_session", "ExecSessionTool", {"exec"}),
    ("nanobot.agent.tools.bg_shell", "BackgroundShellTool", {"exec"}),
    ("nanobot.agent.tools.web", "WebSearchTool", {"network"}),
    ("nanobot.agent.tools.web", "WebFetchTool", {"network"}),
    ("nanobot.agent.tools.filesystem", "ReadFileTool", {"read-workspace"}),
    ("nanobot.agent.tools.filesystem", "ListDirTool", {"read-workspace"}),
    ("nanobot.agent.tools.filesystem", "WriteFileTool", {"write-workspace"}),
    ("nanobot.agent.tools.filesystem", "EditFileTool", {"write-workspace"}),
    ("nanobot.agent.tools.apply_patch", "ApplyPatchTool", {"write-workspace"}),
    ("nanobot.agent.tools.search", "GrepTool", {"read-workspace"}),
    ("nanobot.agent.tools.search", "FindFilesTool", {"read-workspace"}),
])
def test_builtin_tool_effects_are_truthful(module, cls_name, required):
    effects = _tool_effects(module, cls_name)
    assert required <= effects, (cls_name, effects)


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("module, cls_name", [
    ("nanobot.agent.tools.filesystem", "ReadFileTool"),
    ("nanobot.agent.tools.filesystem", "ListDirTool"),
    ("nanobot.agent.tools.search", "GrepTool"),
    ("nanobot.agent.tools.search", "FindFilesTool"),
])
def test_read_only_tools_do_not_claim_none(module, cls_name):
    effects = _tool_effects(module, cls_name)
    assert "none" not in effects
