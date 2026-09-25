"""Smoke test: the slimmed CLI exposes the agent command and none of the comms/UI commands."""

import os
import subprocess
import sys
from pathlib import Path


def test_cli_exposes_only_agent_command(tmp_path: Path) -> None:
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
    for gone in ("gateway", "onboard", "channels", "webui"):
        assert gone not in out.stdout
