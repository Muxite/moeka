"""Spec 005 FR-021..FR-024: the template unit and ``install-service.sh <name>``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._multi_instance import INSTALL_SERVICE, REPO_ROOT, Harness

TEMPLATE = REPO_ROOT / "scripts" / "moeka@.service"


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    # A sudo on PATH that only records: the installer must never call it.
    (harness.bin_dir / "sudo").write_text(
        "#!/usr/bin/env bash\necho \"$*\" >> \"$FAKE_SYSTEMCTL_DIR/sudo.log\"\nexit 0\n"
    )
    (harness.bin_dir / "sudo").chmod(0o755)
    yield harness
    harness.cleanup()


def _env(h: Harness, **extra: str) -> dict[str, str]:
    return h.env(PATH=f"{h.bin_dir}:{h.env()['PATH']}", **extra)


def _new(h: Harness, name: str) -> None:
    h.new(name)
    (h.systemctl_dir / "calls.log").unlink(missing_ok=True)


def _systemctl(h: Harness) -> list[str]:
    return [c for c in h.systemctl_calls() if not c.startswith("loginctl ")]


def test_template_unit_static_contract() -> None:
    text = TEMPLATE.read_text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert "Environment=MOEKA_WORKSPACE=%h/.moeka-%i" in lines
    assert "EnvironmentFile=-%h/.moeka-%i/keys.env" in lines
    assert "ExecStart=@MOEKA_REPO@/bin/moeka.sh --workspace %h/.moeka-%i run" in lines
    assert not any(ln.startswith("WorkingDirectory=") for ln in lines)
    assert "%h/projects/moeka" not in text
    env_files = [ln for ln in lines if ln.startswith("EnvironmentFile=")]
    assert env_files == ["EnvironmentFile=-%h/.moeka-%i/keys.env"]
    original = (REPO_ROOT / "scripts" / "moeka.service").read_text()
    for key in ("Restart=", "RestartSec=", "RestartSteps=", "RestartMaxDelaySec=",
                "StartLimitBurst=", "TimeoutStopSec="):
        ours = [ln for ln in lines if ln.startswith(key)]
        theirs = [ln.strip() for ln in original.splitlines() if ln.strip().startswith(key)]
        assert ours == theirs, key


def test_install_named_renders_and_enables(h: Harness) -> None:
    _new(h, "a")
    units = h.home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    live = units / "moeka.service"
    live.write_bytes(b"[Unit]\nDescription=live unit, do not touch\n")
    before = live.read_bytes()
    result = h.run("a", script=INSTALL_SERVICE, env=_env(h))
    assert result.returncode == 0, result.stderr
    rendered = (units / "moeka@.service").read_text()
    assert "@MOEKA_REPO@" not in rendered
    assert f"ExecStart={REPO_ROOT}/bin/moeka.sh --workspace %h/.moeka-%i run" in rendered
    calls = _systemctl(h)
    assert calls == ["--user daemon-reload", "--user enable --now moeka@a.service"]
    assert live.read_bytes() == before
    assert not any(re.search(r"\b(moeka|nanobot)(\.service)?$", c) for c in calls)
    assert not (h.systemctl_dir / "sudo.log").exists()
    assert "loginctl enable-linger" in result.stdout  # linger off: printed, not run


def test_no_enable_and_dry_run(h: Harness) -> None:
    _new(h, "b")
    result = h.run("b", "--no-enable", script=INSTALL_SERVICE, env=_env(h))
    assert result.returncode == 0
    assert _systemctl(h) == ["--user daemon-reload"]
    unit = h.home / ".config" / "systemd" / "user" / "moeka@.service"
    unit.unlink()
    (h.systemctl_dir / "calls.log").unlink()
    dry = h.run("b", "--dry-run", script=INSTALL_SERVICE, env=_env(h))
    assert dry.returncode == 0
    assert "ExecStart=" in dry.stdout and "enable --now moeka@b.service" in dry.stdout
    assert not unit.exists()
    assert _systemctl(h) == []


def test_missing_instance_and_bad_name(h: Harness) -> None:
    missing = h.run("ghost", script=INSTALL_SERVICE, env=_env(h))
    assert missing.returncode == 1
    assert "moeka.sh new ghost" in missing.stderr
    for bad in ("Bad", "a/b", "-x" * 1, "a" * 33):
        result = h.run(bad, script=INSTALL_SERVICE, env=_env(h))
        assert result.returncode == 2, bad
    assert _systemctl(h) == []


def test_linger_on_prints_nothing_about_it(h: Harness) -> None:
    _new(h, "c")
    result = h.run("c", script=INSTALL_SERVICE, env=_env(h, FAKE_LINGER="yes"))
    assert result.returncode == 0
    assert "enable-linger" not in result.stdout
