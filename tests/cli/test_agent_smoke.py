"""Smoke test: the consolidated CLI exposes the agent command and the gateway host commands."""

import os
import subprocess
import sys
from pathlib import Path


def test_cli_exposes_agent_and_host_commands(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-m", "nanobot", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "agent" in out.stdout
    for host_command in ("gateway", "onboard", "channels", "webui"):
        assert host_command in out.stdout
