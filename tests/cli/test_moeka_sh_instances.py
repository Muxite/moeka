"""Spec 005 FR-001..FR-012: every ``bin/moeka.sh`` command acts on one instance only."""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests._multi_instance import MOEKA_SH, REPO_ROOT, Harness, alive, wait_argv, wait_for

STATUS_KEYS = {
    "name", "workspace", "config", "running", "pid", "manager", "unit", "ports",
    "websocket_socket",
}


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    harness.cleanup()


def _ws(root: Path) -> tuple[str, str]:
    return ("--workspace", str(root))


def _start(h: Harness, root: Path, **env: str) -> int:
    result = h.run(*_ws(root), "start", env=h.env(**env) if env else None)
    assert result.returncode == 0, result.stderr
    pid = h.pid(root)
    wait_argv(h, pid)
    return pid


def test_stop_one_instance_leaves_the_others_running(h: Harness) -> None:
    roots = [h.new(n) for n in ("a", "b", "c")]
    pids = {root.name: _start(h, root) for root in roots}
    configs = [h.config(r) for r in roots]
    gateways = {c["gateway"]["port"] for c in configs}
    apis = {c["api"]["port"] for c in configs}
    sockets = {c["channels"]["websocket"]["unixSocketPath"] for c in configs}
    assert len(gateways) == len(apis) == len(sockets) == 3
    for root, pid in zip(roots, pids.values(), strict=True):
        argv = h.argv_of(pid)
        assert argv[:5] == ["gateway", "--config", str(root / "config.json"),
                            "--workspace", str(root)]

    result = h.run("--workspace", "~/.moeka-a", "stop")
    assert result.returncode == 0, result.stderr
    assert wait_for(lambda: not alive(pids[".moeka-a"]), 10)
    assert alive(pids[".moeka-b"]) and alive(pids[".moeka-c"])
    assert h.pid(roots[1]) == pids[".moeka-b"]
    assert not (roots[0] / "moeka.pid").exists()


def test_status_exit_codes_and_json(h: Harness) -> None:
    root = h.new("s")
    never = h.run(*_ws(root), "status", "--json")
    assert never.returncode == 3
    info = json.loads(never.stdout)
    assert set(info) == STATUS_KEYS
    assert info["running"] is False and info["pid"] is None and info["manager"] is None
    assert info["name"] == "s" and info["unit"] == "moeka@s.service"
    assert info["workspace"] == str(root) and info["config"] == str(root / "config.json")
    assert info["websocket_socket"] == str(root / "run" / "websocket.sock")
    assert info["ports"]["websocket"] is None
    assert isinstance(info["ports"]["gateway"], int) and isinstance(info["ports"]["api"], int)

    pid = _start(h, root)
    running = h.run(*_ws(root), "status", "--json")
    assert running.returncode == 0
    info = json.loads(running.stdout)
    assert info["running"] is True and info["pid"] == pid and info["manager"] == "pid"
    assert h.run(*_ws(root), "status").returncode == 0


def test_status_reports_systemd_when_the_unit_is_active(h: Harness) -> None:
    root = h.new("u")
    h.set_unit_active("moeka@u.service")
    info = json.loads(h.run(*_ws(root), "status", "--json").stdout)
    assert info["running"] is True and info["manager"] == "systemd"


def test_pid_reuse_is_not_signalled(h: Harness) -> None:
    root = h.new("r")
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        (root / "moeka.pid").write_text(f"{stranger.pid}\n")
        result = h.run(*_ws(root), "stop")
        assert result.returncode == 0
        assert "stale" in result.stderr
        time.sleep(0.2)
        assert stranger.poll() is None  # still running: never signalled
        assert not (root / "moeka.pid").exists()
        status = h.run(*_ws(root), "status", "--json")
        assert status.returncode == 3
    finally:
        stranger.kill()
        stranger.wait()


@pytest.mark.parametrize("content", ["", "garbage", "-5", "0"])
def test_garbage_pid_file_is_stale(h: Harness, content: str) -> None:
    root = h.new("g")
    (root / "moeka.pid").write_text(content)
    result = h.run(*_ws(root), "stop")
    assert result.returncode == 0
    assert not (root / "moeka.pid").exists()


def test_sigkill_after_stop_timeout(h: Harness) -> None:
    root = h.new("k")
    pid = _start(h, root, FAKE_NANOBOT_IGNORE_TERM="1")
    started = time.monotonic()
    result = h.run(*_ws(root), "stop", env=h.env(MOEKA_STOP_TIMEOUT_S="1"))
    assert result.returncode == 0
    assert wait_for(lambda: not alive(pid), 5)
    assert time.monotonic() - started < 6
    assert "SIGKILL" in result.stderr


def test_stop_unit_and_stray_pid_process(h: Harness) -> None:
    root = h.new("m")
    pid = _start(h, root)
    h.set_unit_active("moeka@m.service")
    result = h.run(*_ws(root), "stop")
    assert result.returncode == 0
    assert "--user stop moeka@m.service" in h.systemctl_calls()
    assert wait_for(lambda: not alive(pid), 10)
    assert not any("moeka.service" in c and "moeka@" not in c for c in h.systemctl_calls())


def test_start_is_idempotent_per_instance(h: Harness) -> None:
    root = h.new("i")
    pid = _start(h, root)
    again = h.run(*_ws(root), "start")
    assert again.returncode == 0 and "already running" in again.stderr
    assert h.pid(root) == pid
    pids = (h.nanobot_log / "pids").read_text().split()
    assert pids == [str(pid)]


def test_start_does_nothing_when_the_unit_is_active(h: Harness) -> None:
    root = h.new("v")
    h.set_unit_active("moeka@v.service")
    result = h.run(*_ws(root), "start")
    assert result.returncode == 0
    assert not (h.nanobot_log / "pids").exists()


def test_run_holds_the_gateway_lock_and_a_duplicate_exits_0(h: Harness) -> None:
    root = h.new("l")
    pid = _start(h, root)
    from nanobot.config.instances import lock_held

    assert lock_held(root / "gateway.lock")
    duplicate = h.run(*_ws(root), "run", timeout=20)
    assert duplicate.returncode == 0
    assert "refusing" in duplicate.stderr
    assert (h.nanobot_log / "pids").read_text().split() == [str(pid)]
    assert h.pid(root) == pid


def test_run_writes_its_pid_and_execs_with_workspace(h: Harness) -> None:
    root = h.new("x")
    proc = subprocess.Popen(
        ["bash", str(MOEKA_SH), "--workspace", str(root), "run"], env=h.env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_for(lambda: (root / "moeka.pid").exists(), 10)
        assert h.pid(root) == proc.pid
        argv = wait_argv(h, proc.pid)
        assert argv[:5] == ["gateway", "--config", str(root / "config.json"),
                            "--workspace", str(root)]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def test_list_json_lists_every_discovered_instance(h: Harness) -> None:
    default = h.home / ".nanobot"
    default.mkdir()
    (default / "config.json").write_text("{}")
    h.new("a")
    h.new("b")
    result = h.run("list", "--json")
    assert result.returncode == 0
    items = json.loads(result.stdout)
    assert [i["name"] for i in items] == ["default", "a", "b"]
    assert all(set(i) == STATUS_KEYS for i in items)
    assert items[0]["unit"] == "moeka.service"
    plain = h.run("list")
    assert len(plain.stdout.strip().splitlines()) == 3


def test_unit_mapping_for_enable_and_disable(h: Harness) -> None:
    root = h.new("e")
    enabled = h.run(*_ws(root), "enable")
    assert enabled.returncode == 0, enabled.stderr
    calls = h.systemctl_calls()
    assert "--user daemon-reload" in calls and "--user enable --now moeka@e.service" in calls
    disabled = h.run(*_ws(root), "disable")
    assert disabled.returncode == 0
    assert "--user disable --now moeka@e.service" in h.systemctl_calls()
    assert not any(c.endswith(" moeka") or "moeka.service" in c for c in h.systemctl_calls())


def test_logs_reads_the_instance_log(h: Harness) -> None:
    root = h.new("lg")
    (root / "moeka.log").write_text("line-one\nline-two\n")
    result = h.run(*_ws(root), "logs", "-n", "1")
    assert result.returncode == 0 and result.stdout.strip() == "line-two"


def test_doctor_uses_instance_detection(h: Harness) -> None:
    root = h.new("dr")
    pid = _start(h, root)
    result = h.run(*_ws(root), "doctor")
    assert result.returncode == 0, result.stderr
    assert f"PID {pid}" in result.stdout
    assert "moeka@dr.service" in result.stdout


def test_no_pattern_based_process_matching() -> None:
    text = MOEKA_SH.read_text()
    for forbidden in ("pkill", "pgrep", "killall"):
        assert forbidden not in text


def test_unexpanded_workspace_exits_2_for_every_command(h: Harness) -> None:
    env = h.env(MOEKA_WORKSPACE="${MOEKA_WORKSPACE}")
    before = sorted(str(p) for p in h.root.rglob("*"))
    for command in ("start", "stop", "status", "run", "list", "doctor", "logs", "restart"):
        result = h.run(command, env=env)
        assert result.returncode == 2, (command, result.stderr)
        assert "MOEKA_WORKSPACE" in result.stderr
    assert sorted(str(p) for p in h.root.rglob("*")) == before
    assert not (h.home / ".nanobot").exists()


def test_telegram_pair_targets_the_instance_keys_env(h: Harness) -> None:
    root = h.new("tg")
    (root / "keys.env").unlink()
    result = h.run(*_ws(root), "telegram-pair", "123:not-a-real-token", timeout=60)
    keys = root / "keys.env"
    assert keys.is_file() and stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert str(keys) in result.stderr
    assert not (REPO_ROOT / "keys.env").exists() or "keys   : " + str(REPO_ROOT) not in result.stderr


def _fake_repo(tmp_path: Path) -> Path:
    """A copy of the scripts a moeka.sh run needs, so tests can write repo-level env files."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    shutil.copy2(MOEKA_SH, repo / "bin" / "moeka.sh")
    (repo / "nanobot" / "config").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "nanobot" / "config" / "instances.py",
                 repo / "nanobot" / "config" / "instances.py")
    shutil.copytree(REPO_ROOT / "templates", repo / "templates")
    shutil.copytree(REPO_ROOT / "scripts", repo / "scripts")
    return repo


def test_env_file_order_and_repo_env_rule(h: Harness, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    script = repo / "bin" / "moeka.sh"
    (repo / ".env").write_text("FOO=repo-env\nREPO_ONLY=1\nORDER=repo-env\n")
    (repo / "keys.env").write_text("ORDER=repo-keys\n")
    root = h.new("o")
    (root / ".env").write_text("ORDER=root-env\nROOT_ENV=1\nMOEKA_WORKSPACE=/somewhere/else\n")
    (root / "keys.env").write_text("ORDER=root-keys\n")

    def gateway_env(**extra: str) -> tuple[dict[str, str], str]:
        result = h.run(*_ws(root), "start", env=h.env(**extra), script=script)
        assert result.returncode == 0, result.stderr
        pid = h.pid(root)
        wait_argv(h, pid)
        env = h.env_of(pid)
        h.run(*_ws(root), "stop", script=script)
        assert wait_for(lambda: not alive(pid), 10)
        return env, result.stderr

    env, stderr = gateway_env()
    assert env["ORDER"] == "root-keys" and env["ROOT_ENV"] == "1"
    assert "REPO_ONLY" not in env  # Q3: named instances skip the repo files
    assert env["MOEKA_WORKSPACE"] == str(root)  # an env file cannot move the instance
    assert "ignored" in stderr

    env, _ = gateway_env(MOEKA_REPO_ENV="1")
    assert env["REPO_ONLY"] == "1" and env["ORDER"] == "root-keys"

    default = h.home / ".nanobot"
    default.mkdir()
    (default / "config.json").write_text("{}")
    result = h.run("start", env=h.env(), script=script)
    assert result.returncode == 0, result.stderr
    pid = h.pid(default)
    wait_argv(h, pid)
    denv = h.env_of(pid)
    assert denv["REPO_ONLY"] == "1" and denv["ORDER"] == "repo-keys"
    h.run("stop", script=script)


def test_works_without_procps(h: Harness, tmp_path: Path) -> None:
    """Only /proc is used: hide ps/pgrep/pkill from PATH."""
    shim = tmp_path / "shim"
    shim.mkdir()
    for tool in ("bash", "sh", "env", "tr", "cat", "awk", "sed", "realpath", "dirname",
                 "basename", "python3", "flock", "nohup", "sleep", "getconf", "tail", "du",
                 "cut", "id", "mkdir", "rm", "chmod", "mv", "ls", "date", "hostname", "seq"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, shim / tool)
    env = h.env(PATH=str(shim))
    root = h.new("np")
    assert h.run(*_ws(root), "start", env=env).returncode == 0
    pid = h.pid(root)
    wait_argv(h, pid)
    assert h.run(*_ws(root), "status", env=env).returncode == 0
    assert h.run(*_ws(root), "stop", env=env).returncode == 0
    assert wait_for(lambda: not alive(pid), 10)
    assert h.run(*_ws(root), "status", env=env).returncode == 3


def test_restart_of_a_unit_managed_instance_uses_the_unit(h: Harness) -> None:
    root = h.new("ru")
    h.set_unit_active("moeka@ru.service")
    result = h.run(*_ws(root), "restart")
    assert result.returncode == 0
    assert "--user restart moeka@ru.service" in h.systemctl_calls()
    assert not (h.nanobot_log / "pids").exists()


def test_stop_matches_a_config_path_inside_one_argument(h: Harness) -> None:
    root = h.new("w")
    cfg = root / "config.json"
    proc = subprocess.Popen(
        ["bash", "-c", f"trap 'exit 0' TERM; while :; do sleep 0.05; done # --config={cfg}",
         f"gateway --config {cfg}"],
    )
    try:
        (root / "moeka.pid").write_text(f"  {proc.pid}\n")
        info = json.loads(h.run(*_ws(root), "status", "--json").stdout)
        assert info["running"] is True and info["pid"] == proc.pid
        assert h.run(*_ws(root), "stop").returncode == 0
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_pid_file_with_two_numbers_is_stale(h: Harness) -> None:
    root = h.new("t2")
    (root / "moeka.pid").write_text("12 34\n")
    assert h.run(*_ws(root), "stop").returncode == 0
    assert not (root / "moeka.pid").exists()


def test_default_instance_disable_names_moeka_service_only(h: Harness) -> None:
    default = h.home / ".nanobot"
    default.mkdir()
    (default / "config.json").write_text("{}")
    h.set_unit_active("moeka.service")
    assert h.run("disable").returncode == 0
    calls = [c for c in h.systemctl_calls() if not c.startswith("loginctl")]
    named = [c for c in calls if c.split()[-1] not in ("daemon-reload",)]
    assert named and all(c.split()[-1] == "moeka.service" for c in named), calls
