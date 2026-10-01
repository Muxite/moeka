"""Group C: template systemd unit and installer (FR-021..FR-024, US5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO, tree, write_config

TEMPLATE = REPO / "scripts" / "moeka@.service"
LEGACY_UNIT = REPO / "scripts" / "moeka.service"
UNIT_DIR = Path("~/.config/systemd/user")
FORBIDDEN_UNITS = {"moeka", "moeka.service", "nanobot", "nanobot.service"}


def _kv(text: str, key: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(rf"^\s*{re.escape(key)}=(.*)$", text, re.M)]


def unit_tokens(call):
    return [a for a in call if not a.startswith("-")][1:]


# -- FR-021 static template -----------------------------------------------------------------------


@pytest.mark.fr("FR-021")
def test_template_unit_contents():
    assert TEMPLATE.exists()
    text = TEMPLATE.read_text()
    assert "MOEKA_WORKSPACE=%h/.moeka-%i" in " ".join(_kv(text, "Environment"))
    env_files = _kv(text, "EnvironmentFile")
    assert "-%h/.moeka-%i/keys.env" in env_files
    for ef in env_files:
        assert ".moeka-%i" in ef, f"unexpected EnvironmentFile {ef}"
    execs = _kv(text, "ExecStart")
    assert execs and any(
        "@MOEKA_REPO@/bin/moeka.sh --workspace %h/.moeka-%i run" in e for e in execs)
    assert not _kv(text, "WorkingDirectory")
    assert "%h/projects/moeka" not in text
    assert _kv(text, "Restart") == ["on-failure"]


@pytest.mark.fr("FR-021")
def test_template_keeps_backoff_settings():
    text, legacy = TEMPLATE.read_text(), LEGACY_UNIT.read_text()
    for key in ("RestartSec", "RestartSteps", "RestartMaxDelaySec", "StartLimitBurst"):
        if _kv(legacy, key):
            assert _kv(text, key) == _kv(legacy, key), key


# -- FR-022 installer ---------------------------------------------------------------------------


def _instance(h, name="a"):
    return write_config(h.home / f".moeka-{name}")


@pytest.mark.fr("FR-022")
def test_install_named_renders_and_enables(h):
    _instance(h)
    r = h.install("a")
    assert r.returncode == 0, (r.stdout, r.stderr)
    unit = h.home / ".config" / "systemd" / "user" / "moeka@.service"
    assert unit.exists()
    text = unit.read_text()
    assert "@MOEKA_REPO@" not in text
    assert f"{REPO.resolve()}/bin/moeka.sh --workspace %h/.moeka-%i run" in text or \
        f"{REPO}/bin/moeka.sh --workspace %h/.moeka-%i run" in text
    calls = h.systemctl_calls()
    assert ["--user", "daemon-reload"] in calls
    assert ["--user", "enable", "--now", "moeka@a.service"] in calls
    assert not [c for c in h.unrouted() if c and c[0] == "sudo"]
    assert not [c for c in h.loginctl_calls() if "enable-linger" in c]
    assert "enable-linger" in r.stdout + r.stderr, "linger is off: print the command"


@pytest.mark.fr("FR-022")
@pytest.mark.parametrize("name", ["A", "a/b", "-x", "x" * 33])
def test_install_bad_name(h, name):
    r = h.install(name)
    assert r.returncode == 2
    assert not (h.home / ".config" / "systemd" / "user" / "moeka@.service").exists()
    assert not h.systemctl_calls()


@pytest.mark.fr("FR-022")
def test_install_missing_instance(h):
    r = h.install("ghost")
    assert r.returncode == 1
    assert "new ghost" in r.stdout + r.stderr
    assert not (h.home / ".config" / "systemd" / "user" / "moeka@.service").exists()


@pytest.mark.fr("FR-022")
def test_install_no_enable(h):
    _instance(h)
    r = h.install("a", "--no-enable")
    assert r.returncode == 0, r.stderr
    calls = h.systemctl_calls()
    assert ["--user", "daemon-reload"] in calls
    for c in calls:
        assert not {"enable", "start", "restart"} & set(c), c


@pytest.mark.fr("FR-022")
def test_install_dry_run_changes_nothing(h):
    _instance(h)
    before = tree(h.home)
    r = h.install("a", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert tree(h.home) == before
    mutating = {"daemon-reload", "enable", "start", "restart", "stop", "disable"}
    assert not [c for c in h.systemctl_calls() if mutating & set(c)]
    out = r.stdout + r.stderr
    assert "ExecStart=" in out and "moeka@a.service" in out


# -- FR-023 legacy units untouched ---------------------------------------------------------------


@pytest.mark.fr("FR-023")
def test_install_named_never_touches_legacy_units(h):
    _instance(h)
    unit_dir = h.home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    legacy = {"moeka.service": b"[Unit]\nDescription=live moeka\n",
              "nanobot.service": b"[Unit]\nDescription=old nanobot\n"}
    for name, data in legacy.items():
        (unit_dir / name).write_bytes(data)
    h.set_active("moeka.service")
    h.set_active("nanobot.service")
    for args in (["a"], ["a", "--no-enable"], ["a", "--dry-run"]):
        r = h.install(*args)
        assert r.returncode == 0, (args, r.stderr)
    for name, data in legacy.items():
        assert (unit_dir / name).read_bytes() == data
    for c in h.systemctl_calls():
        assert not set(unit_tokens(c)) & FORBIDDEN_UNITS, c


# -- FR-024 moeka.sh enable/disable --------------------------------------------------------------


@pytest.mark.fr("FR-024")
def test_moeka_enable_named_runs_installer(h):
    _instance(h)
    r = h.moeka("--workspace", str(h.home / ".moeka-a"), "enable")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (h.home / ".config" / "systemd" / "user" / "moeka@.service").exists()
    assert ["--user", "enable", "--now", "moeka@a.service"] in h.systemctl_calls()
    for c in h.systemctl_calls():
        assert not set(unit_tokens(c)) & FORBIDDEN_UNITS, c


@pytest.mark.fr("FR-024")
def test_moeka_disable_named(h):
    _instance(h)
    r = h.moeka("--workspace", str(h.home / ".moeka-a"), "disable")
    assert r.returncode == 0
    assert ["--user", "disable", "--now", "moeka@a.service"] in h.systemctl_calls()
    for c in h.systemctl_calls():
        assert not set(unit_tokens(c)) & FORBIDDEN_UNITS, c
