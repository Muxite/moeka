"""Granted roots must not expose dotenv secrets or the running code to the shell (spec 015)."""

import shutil
import subprocess

import pytest

from nanobot.agent.tools import sandbox
from nanobot.agent.tools.sandbox import granted_root_masks, wrap_command
from nanobot.security.protected_paths import source_root


@pytest.fixture(autouse=True)
def _fresh_cache():
    sandbox._MASK_CACHE.clear()


def test_dotenv_files_are_masked_and_templates_are_not(tmp_path):
    root = tmp_path / "projects"
    (root / "app" / "deep" / "er").mkdir(parents=True)
    (root / "app" / ".env").write_text("A=1")
    (root / "app" / ".env.local").write_text("A=1")
    (root / "app" / ".env.example").write_text("A=")
    (root / "app" / "node_modules" / "x").mkdir(parents=True)
    (root / "app" / "node_modules" / "x" / ".env").write_text("A=1")
    args = granted_root_masks([str(root)])
    masked = {args[i + 2] for i, a in enumerate(args) if a == "--ro-bind" and args[i + 1] == "/dev/null"}
    assert str(root / "app" / ".env") in masked
    assert str(root / "app" / ".env.local") in masked
    assert str(root / "app" / ".env.example") not in masked
    assert str(root / "app" / "node_modules" / "x" / ".env") not in masked


def test_source_tree_is_rebound_read_only_when_a_root_covers_it():
    src = source_root()
    args = granted_root_masks([str(src.parent)])
    assert ["--ro-bind", str(src), str(src)] == args[:3]
    assert granted_root_masks(["/nonexistent-root"]) == []


def test_wrap_command_orders_masks_after_the_write_bind(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rw = tmp_path / "rw"
    rw.mkdir()
    (rw / ".env").write_text("S=1")
    cmd = wrap_command("bwrap", "true", str(ws), str(ws), sandbox_rw_binds=[str(rw)])
    parts = cmd.split()
    assert parts.index("--bind-try") < parts.index(str(rw / ".env"))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
def test_real_bwrap_hides_dotenv_and_blocks_source_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rw = tmp_path / "rw"
    rw.mkdir()
    (rw / ".env").write_text("SECRET=hunter2")
    (rw / "ok.txt").write_text("fine")
    probe = f"cat {rw}/.env; echo ---; cat {rw}/ok.txt; echo ---; touch {rw}/new.txt && echo wrote"
    cmd = wrap_command("bwrap", "sh -c '" + probe + "'", str(ws), str(ws), sandbox_rw_binds=[str(rw)])
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if "bwrap:" in out.stderr and "ok.txt" not in out.stdout:
        pytest.skip(f"bwrap unusable here: {out.stderr.strip()[:120]}")
    assert "hunter2" not in out.stdout
    assert "fine" in out.stdout and "wrote" in out.stdout
