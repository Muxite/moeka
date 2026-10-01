"""Spec 005 FR-026, FR-033, FR-034, FR-039: CLI writers and an unexpanded workspace.

Each case runs ``python -m nanobot`` in a subprocess with a temporary HOME (no network,
no real config).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.kernel.instance_lock import acquire_instance_lock

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        "HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8", "PYTHONPATH": str(REPO_ROOT), "NO_COLOR": "1",
        "MOEKA_RUN_DIR": str(home / "run"),
    }
    env.update(extra)
    return env


def _nanobot(home: Path, *args: str, cwd: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nanobot", *args], env=_env(home, **extra), cwd=str(cwd),
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


def _config(path: Path, workspace: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": workspace}},
        "gateway": {"port": 0},
        "channels": {"websocket": {"enabled": False}},
    }), encoding="utf-8")
    return path


COMMANDS = [
    ("gateway",),
    ("serve",),
    ("agent", "-m", "hello"),
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_locked_workspace_exits_3_naming_root_and_holder(tmp_path: Path, command) -> None:
    home = tmp_path / "home"
    root = tmp_path / "inst"
    root.mkdir()
    config = _config(root / "config.json", str(root))
    with acquire_instance_lock(root):
        before = sorted(p.name for p in root.iterdir())
        result = _nanobot(home, *command, "--config", str(config), cwd=tmp_path)
        assert result.returncode == 3, (result.stdout, result.stderr)
        assert str(root.resolve()) in result.stderr
        assert f"pid {os.getpid()}" in result.stderr
        assert sorted(p.name for p in root.iterdir()) == before
        assert not (root.parent / f"{root.name}-sessions").exists()


def test_read_only_commands_work_while_a_writer_runs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "inst"
    root.mkdir()
    config = _config(root / "config.json", str(root))
    with acquire_instance_lock(root):
        status = _nanobot(home, "status", "--config", str(config), cwd=tmp_path)
        assert status.returncode == 0, status.stderr
        sessions = _nanobot(home, "sessions", "--help", cwd=tmp_path)
        assert sessions.returncode == 0


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_unexpanded_workspace_exits_2_and_creates_nothing(tmp_path: Path, command) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = _config(tmp_path / "cfg" / "config.json", "${MOEKA_WORKSPACE}")
    result = _nanobot(home, *command, "--config", str(config), cwd=cwd)  # variable unset
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "MOEKA_WORKSPACE" in result.stderr and "${MOEKA_WORKSPACE}" in result.stderr
    assert not (home / ".nanobot").exists()
    assert list(cwd.iterdir()) == []
    assert not any("${" in p.name for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("value", ["${MOEKA_WORKSPACE}", ""])
def test_literal_or_empty_env_value_is_refused(tmp_path: Path, value: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = _config(tmp_path / "cfg" / "config.json", "${MOEKA_WORKSPACE}")
    result = _nanobot(home, "gateway", "--config", str(config), cwd=cwd,
                      MOEKA_WORKSPACE=value)
    assert result.returncode == 2, result.stdout
    assert not (home / ".nanobot").exists()
    assert list(cwd.iterdir()) == []


def test_workspace_override_makes_the_config_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = _config(tmp_path / "cfg" / "config.json", "${MOEKA_WORKSPACE}")
    ws = tmp_path / "ws"
    result = _nanobot(home, "agent", "-m", "hi", "--config", str(config),
                      "--workspace", str(ws), cwd=tmp_path)
    # Past the workspace check (no provider is configured, so it fails later, not with 2).
    assert result.returncode not in (2, 3), result.stderr
    assert "unexpanded" not in result.stderr


def test_unexpanded_state_home_variable_exits_2(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = _nanobot(home, "gateway", cwd=cwd, MOEKA_WORKSPACE="${MOEKA_WORKSPACE}")
    assert result.returncode == 2, result.stdout
    assert "MOEKA_WORKSPACE" in result.stderr
    assert list(cwd.iterdir()) == [] and not (home / ".nanobot").exists()
