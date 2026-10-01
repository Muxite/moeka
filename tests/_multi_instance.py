"""Shared helpers for the spec 005 (multi-instance) tests.

Not a test module. Builds a fake ``HOME``, a fake ``nanobot`` binary (records its argv
and environment, sleeps, exits on SIGTERM unless told to ignore it), fake
``systemctl``/``loginctl`` commands (log their argv; ``is-active`` exits 3 unless a
marker file says the unit is active) and runs ``bin/moeka.sh`` against them. Nothing
here touches the real ``$HOME``, real systemd units or the network.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOEKA_SH = REPO_ROOT / "bin" / "moeka.sh"
INSTALL_SERVICE = REPO_ROOT / "scripts" / "install-service.sh"

FAKE_NANOBOT = r"""#!/usr/bin/env bash
# Fake nanobot gateway: record argv + env, then idle until SIGTERM.
log_dir="${FAKE_NANOBOT_LOG_DIR:?}"
mkdir -p "$log_dir"
printf '%s\n' "$$" >> "$log_dir/pids"
printf '%s\0' "$@" > "$log_dir/argv.$$"
env > "$log_dir/env.$$"
if [[ "${FAKE_NANOBOT_IGNORE_TERM:-}" == "1" ]]; then
    trap '' TERM
else
    trap 'exit 0' TERM
fi
while true; do sleep 0.05; done
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# Fake systemctl: log argv; is-active/is-enabled answer from marker files.
dir="${FAKE_SYSTEMCTL_DIR:?}"
mkdir -p "$dir"
printf '%s\n' "$*" >> "$dir/calls.log"
args=("$@")
unit="${args[$((${#args[@]} - 1))]}"
unit="${unit%.service}.service"
case " $* " in
    *" is-active "*)
        [[ -e "$dir/active-$unit" ]] && { echo active; exit 0; }
        echo inactive; exit 3 ;;
    *" is-enabled "*)
        [[ -e "$dir/enabled-$unit" ]] && { echo enabled; exit 0; }
        echo disabled; exit 1 ;;
    *" stop "*)
        rm -f "$dir/active-$unit"; exit 0 ;;
esac
exit 0
"""

FAKE_LOGINCTL = r"""#!/usr/bin/env bash
dir="${FAKE_SYSTEMCTL_DIR:?}"
mkdir -p "$dir"
printf 'loginctl %s\n' "$*" >> "$dir/calls.log"
echo "Linger=${FAKE_LINGER:-no}"
exit 0
"""


def _write_exec(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@dataclass
class Harness:
    """A temp HOME plus fakes; ``run(...)`` invokes ``bin/moeka.sh``."""

    root: Path
    home: Path = field(init=False)
    bin_dir: Path = field(init=False)
    nanobot_log: Path = field(init=False)
    systemctl_dir: Path = field(init=False)
    run_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bin_dir = self.root / "fakebin"
        self.bin_dir.mkdir(exist_ok=True)
        self.nanobot_log = self.root / "nanobot-log"
        self.systemctl_dir = self.root / "systemctl"
        self.run_dir = self.root / "run"
        _write_exec(self.bin_dir / "nanobot", FAKE_NANOBOT)
        _write_exec(self.bin_dir / "systemctl", FAKE_SYSTEMCTL)
        _write_exec(self.bin_dir / "loginctl", FAKE_LOGINCTL)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "MOEKA_NANOBOT_BIN": str(self.bin_dir / "nanobot"),
            "MOEKA_SYSTEMCTL": str(self.bin_dir / "systemctl"),
            "MOEKA_LOGINCTL": str(self.bin_dir / "loginctl"),
            "MOEKA_RUN_DIR": str(self.run_dir),
            "MOEKA_STOP_TIMEOUT_S": "5",
            "FAKE_NANOBOT_LOG_DIR": str(self.nanobot_log),
            "FAKE_SYSTEMCTL_DIR": str(self.systemctl_dir),
        }
        env.update(extra)
        return env

    def run(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 60,
        script: Path = MOEKA_SH,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args], env=env or self.env(), capture_output=True,
            text=True, timeout=timeout, cwd=str(self.root),
        )

    def new(self, name: str, *extra: str) -> Path:
        result = self.run("new", name, *extra)
        assert result.returncode == 0, result.stderr
        return self.home / f".moeka-{name}"

    def config(self, root: Path) -> dict:
        return json.loads((root / "config.json").read_text())

    def pid(self, root: Path) -> int:
        return int((root / "moeka.pid").read_text().strip())

    def systemctl_calls(self) -> list[str]:
        path = self.systemctl_dir / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def set_unit_active(self, unit: str, active: bool = True) -> None:
        self.systemctl_dir.mkdir(parents=True, exist_ok=True)
        marker = self.systemctl_dir / f"active-{unit}"
        if active:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)

    def argv_of(self, pid: int) -> list[str]:
        raw = (self.nanobot_log / f"argv.{pid}").read_bytes()
        return [p.decode() for p in raw.split(b"\0") if p]

    def env_of(self, pid: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in (self.nanobot_log / f"env.{pid}").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key] = value
        return out

    def cleanup(self) -> None:
        """SIGKILL every fake gateway this harness started."""
        pids_file = self.nanobot_log / "pids"
        if not pids_file.exists():
            return
        for line in pids_file.read_text().split():
            try:
                os.kill(int(line), signal.SIGKILL)
            except (ProcessLookupError, ValueError, PermissionError):
                pass


def alive(pid: int) -> bool:
    """True when *pid* runs (a zombie counts as gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    state = stat.rsplit(")", 1)[1].split()[0]
    return state not in ("Z", "X")


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def wait_argv(h: Harness, pid: int, timeout: float = 10.0) -> list[str]:
    assert wait_for(lambda: (h.nanobot_log / f"argv.{pid}").exists(), timeout), (
        f"fake gateway {pid} never recorded its argv"
    )
    return h.argv_of(pid)
