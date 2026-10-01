"""Group A: per-instance control script bin/moeka.sh (FR-001..FR-012, SC-002)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import MOEKA_SH, REPO, cmdline, flock_held, pid_alive, tree, wait_for, write_config

FR6_KEYS = {"name", "workspace", "config", "running", "pid", "manager", "unit", "ports",
            "websocket_socket"}


def named(h, name: str, **kw) -> tuple[Path, Path]:
    root = h.home / f".moeka-{name}"
    return root, write_config(root, **kw)


def status(h, root: Path, *extra: str, env=None):
    r = h.moeka("--workspace", str(root), "status", "--json", *extra, env=env)
    try:
        doc = json.loads(r.stdout.strip())
    except ValueError:
        doc = None
    return r, doc


def start(h, root: Path, env=None) -> int:
    r = h.moeka("--workspace", str(root), "start", env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    rec = h.wait_gateway(root / "config.json")
    assert rec, f"no fake gateway for {root}: {r.stdout} {r.stderr}"
    return int(rec["pid"])


def unit_tokens(call: list[str]) -> list[str]:
    return [a for a in call if not a.startswith("-")][1:]


def spawn_decoy(h, config: str = "/nonexistent/config.json") -> int:
    """An unrelated process whose cmdline contains 'nanobot gateway' (pkill -f bait)."""
    p = h.popen([sys.executable, "-c", "import time; time.sleep(120)", "nanobot", "gateway",
                 "--config", config])
    h.track(p.pid)
    assert wait_for(lambda: pid_alive(p.pid), 5)
    return p.pid


# -- FR-001 instance selection ----------------------------------------------------------------


@pytest.mark.fr("FR-001", "FR-006")
def test_workspace_flag_beats_env_and_tilde_expands(h):
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    env = h.env(MOEKA_WORKSPACE=str(b))
    r, doc = status(h, a, env=env)
    assert doc is not None, (r.stdout, r.stderr)
    assert doc["workspace"] == str(a)
    r = h.moeka("status", "--json", env=env)
    assert json.loads(r.stdout.strip())["workspace"] == str(b)
    r = h.moeka("--workspace", "~/.moeka-a", "status", "--json")
    assert json.loads(r.stdout.strip())["workspace"] == str(a)
    r = h.moeka("status", "--json", env=h.env(MOEKA_WORKSPACE="~/.moeka-b"))
    assert json.loads(r.stdout.strip())["workspace"] == str(b)


@pytest.mark.fr("FR-001", "FR-006")
def test_default_instance_is_home_nanobot(h):
    default = h.home / ".nanobot"
    write_config(default)
    r = h.moeka("status", "--json")
    doc = json.loads(r.stdout.strip())
    assert doc["workspace"] == str(default)
    assert doc["name"] == "default"


@pytest.mark.fr("FR-001")
def test_env_file_cannot_redirect_instance(h):
    a, cfg_a = named(h, "a")
    b, _ = named(h, "b")
    (a / ".env").write_text(f"MOEKA_WORKSPACE={b}\n")
    (a / "keys.env").write_text(f"MOEKA_WORKSPACE={b}\n")
    r, doc = status(h, a)
    assert doc is not None and doc["workspace"] == str(a), (r.stdout, r.stderr)
    assert "MOEKA_WORKSPACE" in r.stderr, "a warning naming MOEKA_WORKSPACE must be printed"
    # And `run` still targets a's own config.
    p = h.popen(["bash", str(MOEKA_SH), "--workspace", str(a), "run"])
    rec = h.wait_gateway(cfg_a)
    assert rec, "run did not start a's gateway"
    assert str(b / "config.json") not in rec["argv"]


# -- FR-002 unexpanded workspace --------------------------------------------------------------

UNEXPANDED_CMDS = [["status"], ["status", "--json"], ["start"], ["run"], ["stop"],
                   ["restart"], ["doctor"], ["logs"], ["list"], ["telegram-pair"],
                   ["new", "zz"], ["enable"], ["disable"]]


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("cmd", UNEXPANDED_CMDS, ids=lambda c: "-".join(c))
def test_unexpanded_workspace_exits_2_and_creates_nothing(h, cmd):
    write_config(h.home / ".nanobot")
    before = tree(h.home, REPO / "bin", REPO / "scripts")
    r = h.moeka(*cmd, env=h.env(MOEKA_WORKSPACE="${MOEKA_WORKSPACE}"))
    assert r.returncode == 2, (cmd, r.returncode, r.stdout, r.stderr)
    assert "MOEKA_WORKSPACE" in r.stderr
    after = tree(h.home, REPO / "bin", REPO / "scripts")
    assert after == before
    assert not list(REPO.glob("*${*")), "a literal ${...} directory was created in the repo"
    assert not list(h.aux.glob("*${*"))
    assert not h.gateway_records()
    assert not [c for c in h.systemctl_calls() if c and c[-1] != "--quiet"
                and any(v in c for v in ("start", "stop", "enable", "disable", "restart"))]


@pytest.mark.fr("FR-002")
def test_unexpanded_workspace_flag_exits_2(h):
    r = h.moeka("--workspace", "${NOPE}/x", "status")
    assert r.returncode == 2 and "MOEKA_WORKSPACE" in r.stderr
    assert not list(h.home.rglob("*${*"))


# -- FR-003 env file order + Q3 ---------------------------------------------------------------


def _run_and_capture_env(h, root: Path, env=None) -> dict:
    h.popen(["bash", str(MOEKA_SH), "--workspace", str(root), "run"], env=env)
    rec = h.wait_gateway(root / "config.json")
    assert rec, "run did not start the (fake) gateway"
    return rec["env"]


def _write_layered_env(repo_env_files, root: Path) -> None:
    repo_env_files(".env", "HT_A=repo_env\nHT_B=repo_env\n")
    repo_env_files("keys.env", "HT_B=repo_keys\nHT_C=repo_keys\n")
    (root / ".env").write_text("HT_C=root_env\nHT_D=root_env\n")
    (root / "keys.env").write_text("HT_D=root_keys\n")


@pytest.mark.fr("FR-003")
def test_named_instance_skips_repo_env_files(h, repo_env_files):
    a, _ = named(h, "a")
    _write_layered_env(repo_env_files, a)
    env = _run_and_capture_env(h, a)
    assert "HT_A" not in env and "HT_B" not in env
    assert env.get("HT_C") == "root_env"
    assert env.get("HT_D") == "root_keys"


@pytest.mark.fr("FR-003")
def test_repo_env_opt_in_and_order(h, repo_env_files):
    a, _ = named(h, "a")
    _write_layered_env(repo_env_files, a)
    env = _run_and_capture_env(h, a, env=h.env(MOEKA_REPO_ENV="1"))
    assert env.get("HT_A") == "repo_env"
    assert env.get("HT_B") == "repo_keys"
    assert env.get("HT_C") == "root_env"
    assert env.get("HT_D") == "root_keys"


@pytest.mark.fr("FR-003")
def test_default_instance_loads_repo_env_files(h, repo_env_files):
    d = h.home / ".nanobot"
    write_config(d)
    _write_layered_env(repo_env_files, d)
    env = _run_and_capture_env(h, d)
    assert env.get("HT_A") == "repo_env"
    assert env.get("HT_B") == "repo_keys"
    assert env.get("HT_C") == "root_env"
    assert env.get("HT_D") == "root_keys"


@pytest.mark.fr("FR-003")
def test_registered_instance_skips_repo_env_files(h, repo_env_files):
    custom = h.aux / "custom-inst"
    write_config(custom)
    reg = h.home / ".config" / "moeka" / "instances"
    reg.parent.mkdir(parents=True)
    reg.write_text(f"{custom}\n")
    _write_layered_env(repo_env_files, custom)
    env = _run_and_capture_env(h, custom)
    assert "HT_A" not in env and "HT_B" not in env
    assert env.get("HT_D") == "root_keys"


# -- FR-004 stop ------------------------------------------------------------------------------


@pytest.mark.fr("FR-004", "SC-002")
def test_stop_one_instance_leaves_others_running(h):
    roots = [named(h, n)[0] for n in ("a", "b", "c")]
    pids = {r: start(h, r) for r in roots}
    decoy = spawn_decoy(h)
    t0 = time.monotonic()
    r = h.moeka("--workspace", str(roots[1]), "stop")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert wait_for(lambda: not pid_alive(pids[roots[1]]), 3 + 5)
    assert time.monotonic() - t0 <= 3 + 5
    assert pid_alive(pids[roots[0]]) and pid_alive(pids[roots[2]])
    assert pid_alive(decoy), "stop signalled an unrelated 'nanobot gateway' process"
    assert not (roots[1] / "moeka.pid").exists() or not pid_alive(
        int((roots[1] / "moeka.pid").read_text().strip() or 0))


@pytest.mark.fr("FR-004", "SC-002")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_sc002_ten_repetitions(h):
    roots = [named(h, n)[0] for n in ("a", "b", "c")]
    for rep in range(10):
        pids = {r: start(h, r) for r in roots}
        victim = roots[rep % 3]
        t0 = time.monotonic()
        r = h.moeka("--workspace", str(victim), "stop")
        assert r.returncode == 0
        assert wait_for(lambda: not pid_alive(pids[victim]), 8), f"rep {rep}"
        assert time.monotonic() - t0 <= 3 + 5
        for other in roots:
            if other != victim:
                assert pid_alive(pids[other]), f"rep {rep}: {other.name} died"
        for other in roots:
            if other != victim:
                assert h.moeka("--workspace", str(other), "stop").returncode == 0
                assert wait_for(lambda o=other: not pid_alive(pids[o]), 8)


@pytest.mark.fr("FR-004")
def test_stop_sigkills_after_timeout(h):
    a, _ = named(h, "a")
    env = h.env(FAKE_IGNORE_TERM="1", MOEKA_STOP_TIMEOUT_S="2")
    pid = start(h, a, env=env)
    t0 = time.monotonic()
    r = h.moeka("--workspace", str(a), "stop", env=env)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0
    assert wait_for(lambda: not pid_alive(pid), 3)
    assert elapsed >= 1.5, "SIGKILL was sent before MOEKA_STOP_TIMEOUT_S elapsed"
    assert elapsed <= 2 + 5


@pytest.mark.fr("FR-004")
def test_stop_does_not_signal_reused_pid(h):
    a, _ = named(h, "a")
    decoy = spawn_decoy(h)  # live, but its cmdline does not contain a's config path
    (a / "moeka.pid").write_text(f"{decoy}\n")
    r = h.moeka("--workspace", str(a), "stop")
    assert r.returncode == 0
    time.sleep(0.3)
    assert pid_alive(decoy)
    assert not (a / "moeka.pid").exists(), "stale PID file must be removed"


@pytest.mark.fr("FR-004")
def test_stop_does_not_signal_other_instances_gateway_via_pid_file(h):
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    pid_b = start(h, b)
    (a / "moeka.pid").write_text(f"{pid_b}\n")
    r = h.moeka("--workspace", str(a), "stop")
    assert r.returncode == 0
    time.sleep(0.3)
    assert pid_alive(pid_b)
    assert not (a / "moeka.pid").exists()


@pytest.mark.fr("FR-004")
@pytest.mark.parametrize("content", ["", "garbage\n", "-1\n", "99999999\n"])
def test_stop_with_garbage_pid_file(h, content):
    a, _ = named(h, "a")
    (a / "moeka.pid").write_text(content)
    r = h.moeka("--workspace", str(a), "stop")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (a / "moeka.pid").exists()


@pytest.mark.fr("FR-004")
def test_stop_nothing_running_exits_0(h):
    a, _ = named(h, "a")
    r = h.moeka("--workspace", str(a), "stop")
    assert r.returncode == 0


@pytest.mark.fr("FR-004", "FR-012")
def test_stop_named_unit_and_stray_pid(h):
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    pid_a = start(h, a)
    pid_b = start(h, b)
    h.set_active("moeka@a.service")
    h.set_active("moeka.service")  # the default instance's unit: must not be touched
    h.clear_systemctl_calls()  # b's own start legitimately queried moeka@b
    r = h.moeka("--workspace", str(a), "stop")
    assert r.returncode == 0
    calls = h.systemctl_calls()
    stops = [c for c in calls if "stop" in c]
    assert any(set(unit_tokens(c)) & {"moeka@a", "moeka@a.service"} for c in stops), calls
    for c in calls:
        for u in unit_tokens(c):
            assert u in ("moeka@a", "moeka@a.service"), f"systemctl touched {u}: {c}"
    assert wait_for(lambda: not pid_alive(pid_a), 8), "stray PID-file process not stopped"
    assert pid_alive(pid_b)


@pytest.mark.fr("FR-004")
def test_static_no_pattern_kill(h):
    text = MOEKA_SH.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\bpkill\b", code)
    assert not re.search(r"\bpgrep\s+(-\w*\s+)*-\w*f", code) and not re.search(r"pgrep -f", code)
    assert not re.search(r"\bkillall\b", code)


# -- FR-005 / FR-006 status ------------------------------------------------------------------


@pytest.mark.fr("FR-005", "FR-006")
def test_status_never_started(h):
    a, cfg = named(h, "a", gateway=18901, api=18902)
    r, doc = status(h, a)
    assert r.returncode == 3
    assert doc is not None and set(doc) >= FR6_KEYS
    assert doc["running"] is False and doc["pid"] is None
    assert doc["name"] == "a"
    assert doc["config"] == str(cfg)
    r = h.moeka("--workspace", str(a), "status")
    assert r.returncode == 3


@pytest.mark.fr("FR-005", "FR-006")
def test_status_running_via_pid(h):
    a, cfg = named(h, "a", gateway=18911, api=18912)
    b, _ = named(h, "b")
    pid = start(h, a)
    r, doc = status(h, a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert doc["running"] is True and doc["pid"] == pid and doc["manager"] == "pid"
    assert doc["workspace"] == str(a)
    rb, docb = status(h, b)
    assert rb.returncode == 3 and docb["running"] is False and docb["pid"] is None


@pytest.mark.fr("FR-005")
def test_status_running_via_gateway_lock(h):
    a, _ = named(h, "a")
    lock = a / "gateway.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r, doc = status(h, a)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert doc["running"] is True
    finally:
        os.close(fd)
    r, doc = status(h, a)
    assert r.returncode == 3


@pytest.mark.fr("FR-005", "FR-006", "FR-012")
def test_status_running_via_unit(h):
    a, _ = named(h, "a")
    h.set_active("moeka@a.service")
    r, doc = status(h, a)
    assert r.returncode == 0
    assert doc["running"] is True and doc["manager"] == "systemd"
    assert doc["unit"] == "moeka@a.service"
    # Another instance's unit being active does not make b run.
    b, _ = named(h, "b")
    rb, docb = status(h, b)
    assert rb.returncode == 3 and docb["unit"] == "moeka@b.service"


@pytest.mark.fr("FR-006", "FR-012")
def test_status_json_ports_socket_and_units(h):
    a, cfg = named(h, "a", gateway=18921, api=18922)
    r, doc = status(h, a)
    assert doc["ports"] == {"gateway": 18921, "api": 18922, "websocket": None}
    assert doc["websocket_socket"] == str(a / "run" / "websocket.sock")
    t, _ = named(h, "t", gateway=18931, api=18932, ws_socket=False, ws_port=18933)
    r, doc = status(h, t)
    assert doc["ports"] == {"gateway": 18931, "api": 18932, "websocket": 18933}
    assert doc["websocket_socket"] is None
    d = h.home / ".nanobot"
    write_config(d)
    r, doc = status(h, d)
    assert doc["unit"] == "moeka.service" and doc["name"] == "default"
    custom = h.aux / "reg1"
    write_config(custom)
    r, doc = status(h, custom)
    assert doc["unit"] is None


@pytest.mark.fr("FR-005")
def test_status_without_procps(h):
    """ps/pgrep/pkill are shadowed by failing guards on PATH (procps absent)."""
    a, _ = named(h, "a")
    start(h, a)
    r, doc = status(h, a)
    assert r.returncode == 0 and doc["running"] is True


# -- FR-007 doctor / logs ---------------------------------------------------------------------


@pytest.mark.fr("FR-007")
def test_doctor_reports_only_own_instance(h):
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    pid_a = start(h, a)
    decoy = spawn_decoy(h, config=str(b / "config.json"))
    rb = h.moeka("--workspace", str(b), "doctor")
    assert str(pid_a) not in rb.stdout + rb.stderr
    assert str(decoy) not in rb.stdout + rb.stderr
    ra = h.moeka("--workspace", str(a), "doctor")
    assert str(pid_a) in ra.stdout + ra.stderr


@pytest.mark.fr("FR-007")
def test_logs_reads_own_log_file(h):
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    (a / "moeka.log").write_text("line-from-A\n")
    (b / "moeka.log").write_text("line-from-B\n")
    h.set_active("moeka.service")  # the default unit is active; not a's unit
    r = h.moeka("--workspace", str(a), "logs")
    assert "line-from-A" in r.stdout and "line-from-B" not in r.stdout
    assert "FAKE-JOURNAL-OUTPUT" not in r.stdout


@pytest.mark.fr("FR-007")
def test_logs_reads_own_unit_journal_when_active(h):
    a, _ = named(h, "a")
    (a / "moeka.log").write_text("line-from-A\n")
    h.set_active("moeka@a.service")
    h.moeka("--workspace", str(a), "logs")
    journal = [c for c in h.unrouted() if c and c[0] == "journalctl"]
    assert journal, "logs did not read the journal of the active unit"
    joined = " ".join(" ".join(c) for c in journal)
    assert "moeka@a" in joined
    assert not re.search(r"(-u|--unit)[ =]moeka(\.service)?\b(?!@)", joined)


@pytest.mark.fr("FR-007")
def test_static_doctor_logs_no_pgrep(h):
    text = MOEKA_SH.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "pgrep" not in code


# -- FR-008 run --------------------------------------------------------------------------------


@pytest.mark.fr("FR-008")
def test_run_pid_lock_and_argv(h):
    a, cfg = named(h, "a")
    p = h.popen(["bash", str(MOEKA_SH), "--workspace", str(a), "run"])
    rec = h.wait_gateway(cfg)
    assert rec
    argv = rec["argv"]
    assert argv[0] == "gateway"
    assert argv[argv.index("--config") + 1] == str(cfg)
    assert argv[argv.index("--workspace") + 1] == str(a)
    assert int((a / "moeka.pid").read_text().strip()) == int(rec["pid"])
    assert int(rec["pid"]) == p.pid, "run must exec the gateway (same PID)"
    assert flock_held(a / "gateway.lock")
    # A duplicate run exits 0 and starts nothing.
    n_before = len(h.gateway_records(cfg))
    r = h.moeka("--workspace", str(a), "run", timeout=15)
    assert r.returncode == 0
    time.sleep(0.5)
    assert len(h.gateway_records(cfg)) == n_before
    assert pid_alive(p.pid)


@pytest.mark.fr("FR-008")
def test_run_honours_moeka_config(h):
    a, _ = named(h, "a")
    alt = a / "alt.json"
    alt.write_text((a / "config.json").read_text())
    h.popen(["bash", str(MOEKA_SH), "--workspace", str(a), "run"],
            env=h.env(MOEKA_CONFIG=str(alt)))
    rec = h.wait_gateway(alt)
    assert rec
    argv = rec["argv"]
    assert argv[argv.index("--config") + 1] == str(alt)
    assert argv[argv.index("--workspace") + 1] == str(a)


# -- FR-009 start idempotence -------------------------------------------------------------------


@pytest.mark.fr("FR-009")
def test_start_twice_starts_one_process(h):
    a, cfg = named(h, "a")
    start(h, a)
    r = h.moeka("--workspace", str(a), "start")
    assert r.returncode == 0
    time.sleep(0.7)
    live = [x for x in h.gateway_records(cfg) if pid_alive(int(x["pid"]))]
    assert len(live) == 1


@pytest.mark.fr("FR-009")
def test_start_when_unit_active_starts_nothing(h):
    a, cfg = named(h, "a")
    h.set_active("moeka@a.service")
    r = h.moeka("--workspace", str(a), "start")
    assert r.returncode == 0
    time.sleep(0.7)
    assert not h.gateway_records(cfg)


@pytest.mark.fr("FR-009")
def test_start_other_instance_while_one_runs(h):
    a, cfg_a = named(h, "a")
    b, cfg_b = named(h, "b")
    start(h, a)
    h.set_active("moeka.service")  # default unit active must not block named instances
    pid_b = start(h, b)
    assert pid_alive(pid_b)


@pytest.mark.fr("FR-009", "FR-005")
def test_start_when_gateway_lock_held_starts_nothing(h):
    a, cfg = named(h, "a")
    fd = os.open(a / "gateway.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = h.moeka("--workspace", str(a), "start")
        assert r.returncode == 0
        time.sleep(0.7)
        assert not [x for x in h.gateway_records(cfg) if pid_alive(int(x["pid"]))]
    finally:
        os.close(fd)


# -- FR-010 telegram-pair ----------------------------------------------------------------------


@pytest.mark.fr("FR-010")
def test_telegram_pair_targets_instance_keys(h):
    a, _ = named(h, "a")
    repo_keys = REPO / "keys.env"
    existed = repo_keys.exists()
    before = repo_keys.read_bytes() if existed else None
    # Offline: the Bot API is unreachable through a dead proxy, so pairing fails at getMe.
    env = h.env()
    r = h.moeka("--workspace", str(a), "telegram-pair", "123456:FAKE-TOKEN", env=env,
                timeout=60)
    assert repo_keys.exists() == existed
    if existed:
        assert repo_keys.read_bytes() == before
    out = r.stdout + r.stderr
    assert str(a / "keys.env") in out, "the pairing target shown must be <root>/keys.env"
    assert str(repo_keys) not in out
    if (a / "keys.env").exists():
        assert (a / "keys.env").stat().st_mode & 0o777 == 0o600


# -- FR-011 list --------------------------------------------------------------------------------


@pytest.mark.fr("FR-011")
def test_list_json_discovers_instances(h):
    d = h.home / ".nanobot"
    write_config(d)
    a, _ = named(h, "a")
    b, _ = named(h, "b")
    (h.home / ".moeka-noconfig").mkdir()
    custom = h.aux / "reg-x"
    write_config(custom)
    reg = h.home / ".config" / "moeka" / "instances"
    reg.parent.mkdir(parents=True)
    reg.write_text(f"{custom}\n{h.aux / 'reg-missing'}\n")
    pid_a = start(h, a)
    r = h.moeka("list", "--json")
    assert r.returncode == 0, (r.stdout, r.stderr)
    doc = json.loads(r.stdout.strip())
    assert isinstance(doc, list)
    assert sorted(x["workspace"] for x in doc) == sorted(map(str, (d, a, b, custom)))
    for x in doc:
        assert set(x) >= FR6_KEYS
    by_ws = {x["workspace"]: x for x in doc}
    assert by_ws[str(a)]["running"] is True and by_ws[str(a)]["pid"] == pid_a
    assert by_ws[str(b)]["running"] is False
    r = h.moeka("list")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    for ws in map(str, (d, a, b, custom)):
        pat = re.compile(re.escape(ws) + r"(?![\w.-])")
        assert sum(bool(pat.search(ln)) for ln in lines) == 1, (ws, r.stdout)


@pytest.mark.fr("FR-011")
def test_list_json_empty(h):
    r = h.moeka("list", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout.strip()) == []


# -- FR-012 unit addressing ------------------------------------------------------------------------


@pytest.mark.fr("FR-012", "FR-024")
def test_disable_named_addresses_only_its_unit(h):
    a, _ = named(h, "a")
    h.set_active("moeka@a.service")
    h.clear_systemctl_calls()
    r = h.moeka("--workspace", str(a), "disable")
    assert r.returncode == 0, (r.stdout, r.stderr)
    calls = h.systemctl_calls()
    assert any(c[:1] == ["--user"] and "disable" in c and "--now" in c
               and "moeka@a.service" in c for c in calls), calls
    for c in calls:
        for u in unit_tokens(c):
            assert u in ("moeka@a", "moeka@a.service"), c


@pytest.mark.fr("FR-012")
def test_restart_named_unit(h):
    a, _ = named(h, "a")
    h.set_active("moeka@a.service")
    h.clear_systemctl_calls()
    h.moeka("--workspace", str(a), "restart")
    for c in h.systemctl_calls():
        for u in unit_tokens(c):
            assert u in ("moeka@a", "moeka@a.service"), c


@pytest.mark.fr("FR-012")
def test_registered_instance_has_no_unit(h):
    custom = h.aux / "reg-y"
    write_config(custom)
    pid = start(h, custom)
    h.set_active("moeka.service")
    h.clear_systemctl_calls()
    r = h.moeka("--workspace", str(custom), "stop")
    assert r.returncode == 0
    for c in h.systemctl_calls():
        assert not set(unit_tokens(c)) & {"moeka", "moeka.service", "nanobot",
                                           "nanobot.service"}, c
    assert wait_for(lambda: not pid_alive(pid), 8)


@pytest.mark.fr("FR-012")
def test_default_instance_unit_is_moeka_service(h):
    d = h.home / ".nanobot"
    write_config(d)
    h.set_active("moeka.service")
    h.clear_systemctl_calls()
    r = h.moeka("stop")
    assert r.returncode == 0
    stops = [c for c in h.systemctl_calls() if "stop" in c]
    assert any(set(unit_tokens(c)) & {"moeka", "moeka.service"} for c in stops)
    for c in h.systemctl_calls():
        for u in unit_tokens(c):
            assert not u.startswith("moeka@"), c


@pytest.mark.fr("FR-012")
def test_no_unrouted_systemctl(h):
    a, _ = named(h, "a")
    h.set_active("moeka@a.service")
    for cmd in (["status"], ["stop"], ["start"], ["logs"], ["doctor"]):
        h.moeka("--workspace", str(a), *cmd)
    assert not [c for c in h.unrouted() if c and c[0] in ("systemctl", "sudo")]
