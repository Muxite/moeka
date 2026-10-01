"""Group D: fail fast on an unexpanded workspace (FR-025, FR-026)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from conftest import free_port, tree, wait_for

PLACEHOLDER = "${MOEKA_WORKSPACE}"


def _config(path: Path, workspace: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": workspace}},
        "gateway": {"host": "127.0.0.1", "port": free_port()},
        "api": {"host": "127.0.0.1", "port": free_port()},
        "channels": {"websocket": {"unixSocketPath": str(path.parent / "ws.sock")}},
    }))
    return path


@pytest.mark.fr("FR-025")
@pytest.mark.parametrize("value", [PLACEHOLDER, "${OTHER_VAR}/inst", "~/${X}"])
def test_workspace_path_raises(h, value):
    from nanobot.config.schema import Config, UnexpandedWorkspaceError

    assert issubclass(UnexpandedWorkspaceError, ValueError)
    cfg = Config.model_validate({"agents": {"defaults": {"workspace": value}}})
    with pytest.raises(UnexpandedWorkspaceError) as info:
        _ = cfg.workspace_path
    msg = str(info.value)
    assert "MOEKA_WORKSPACE" in msg
    assert value.replace("~", str(h.home), 1) in msg or value in msg
    assert not (h.home / ".nanobot").exists()


@pytest.mark.fr("FR-025")
def test_no_fallback_to_state_home(h, monkeypatch):
    from nanobot.config.schema import Config, UnexpandedWorkspaceError

    monkeypatch.setenv("MOEKA_WORKSPACE", str(h.home / "elsewhere"))
    cfg = Config.model_validate({"agents": {"defaults": {"workspace": "${NOT_SET_ANYWHERE}"}}})
    with pytest.raises(UnexpandedWorkspaceError):
        _ = cfg.workspace_path


@pytest.mark.fr("FR-025")
def test_other_missing_vars_still_warn_only(h):
    from nanobot.config.loader import load_config

    root = h.home / "inst"
    path = _config(root / "config.json", str(root))
    data = json.loads(path.read_text())
    data["providers"] = {"openai": {"apiKey": "${HELDOUT_SURELY_UNSET_KEY}"}}
    path.write_text(json.dumps(data))
    cfg = load_config(path)
    assert cfg.workspace_path == root


def _no_bad_dirs(h, *extra: Path) -> None:
    for base in (h.home, h.aux, *extra):
        assert not [p for p in base.rglob("*") if "${" in p.name], f"literal ${{ dir in {base}"
    assert not (h.home / ".nanobot").exists()


CMDS = {
    "gateway": ["gateway"],
    "serve": ["serve"],
    "agent-m": ["agent", "-m", "hello"],
}


@pytest.mark.fr("FR-026")
@pytest.mark.timeout(180)
@pytest.mark.parametrize("cmd", list(CMDS), ids=list(CMDS))
def test_cli_exits_2_on_unexpanded_workspace(h, cmd):
    cfg = _config(h.aux / "cfgdir" / "config.json", PLACEHOLDER)
    cwd = h.aux / "cwd"
    cwd.mkdir()
    r = h.nanobot_cli(*CMDS[cmd], "--config", str(cfg), cwd=cwd,
                      env=h.env(MOEKA_WORKSPACE=None), timeout=90)
    assert r.returncode == 2, (cmd, r.returncode, r.stderr[-2000:])
    assert "MOEKA_WORKSPACE" in r.stderr
    _no_bad_dirs(h, cwd)


@pytest.mark.fr("FR-026")
@pytest.mark.timeout(180)
@pytest.mark.parametrize("cmd", list(CMDS), ids=list(CMDS))
def test_workspace_override_makes_config_usable(h, cmd):
    """With --workspace the same config passes the check: the command proceeds past it.

    The instance lock on the override root is pre-held so a correct implementation stops
    quickly (exit 3) instead of starting a real gateway; any exit except 2 passes.
    """
    cfg = _config(h.aux / "cfgdir" / "config.json", PLACEHOLDER)
    good = h.aux / "good-ws"
    good.mkdir()
    ready = h.aux / "ready.json"
    h.python("hold_flock", str(good / ".instance.lock"), str(ready))
    assert wait_for(ready.exists, 20)
    cwd = h.aux / "cwd"
    cwd.mkdir()
    r = h.nanobot_cli(*CMDS[cmd], "--config", str(cfg), "--workspace", str(good), cwd=cwd,
                      env=h.env(MOEKA_WORKSPACE=None), timeout=40)
    assert r.returncode != 2, r.stderr[-2000:]
    assert "UnexpandedWorkspace" not in r.stderr
    _no_bad_dirs(h, cwd)
