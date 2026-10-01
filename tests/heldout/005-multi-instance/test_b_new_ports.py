"""Group B: moeka.sh new and port assignment (FR-013..FR-020, SC-007)."""

from __future__ import annotations

import json
import socket
import stat
import time
from pathlib import Path

import pytest

from conftest import REPO, port_bindable, tree, write_config

AUTO_RANGE = list(range(18800, 19791, 10))
DEFAULTS = {"gateway": 18790, "api": 8900, "websocket": 8765}


def load(root: Path) -> dict:
    return json.loads((root / "config.json").read_text())


def ports_of(cfg: dict) -> set[int]:
    used = {cfg["gateway"]["port"], cfg["api"]["port"]}
    ws = cfg.get("channels", {}).get("websocket") or {}
    if not ws.get("unixSocketPath") and ws.get("port"):
        used.add(ws["port"])
    return used


def ws_of(cfg: dict) -> dict:
    return cfg.get("channels", {}).get("websocket") or {}


def expected_base(used: set[int], ws_tcp: bool = False) -> int | None:
    for b in AUTO_RANGE:
        need = [b, b + 1] + ([b + 2] if ws_tcp else [])
        if any(p in used for p in need):
            continue
        if all(port_bindable(p) for p in need):
            return b
    return None


# -- FR-013 names, targets, registry -------------------------------------------------------------


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize("name", ["A", "a/b", "x" * 33, "-a", "_a", "a b", "a.b", "Ab", ""])
def test_bad_names_exit_2(h, name):
    before = tree(h.home)
    r = h.moeka("new", name)
    assert r.returncode == 2, (name, r.returncode, r.stderr)
    assert tree(h.home) == before


@pytest.mark.fr("FR-013", "FR-014")
@pytest.mark.parametrize("name", ["a", "0a_-", "x" * 32])
def test_good_names_create_default_target(h, name):
    r = h.moeka("new", name)
    assert r.returncode == 0, (r.stdout, r.stderr)
    root = h.home / f".moeka-{name}"
    cfg = load(root)
    assert cfg["agents"]["defaults"]["workspace"] == str(root)


@pytest.mark.fr("FR-013")
def test_non_empty_target_refused(h):
    root = h.home / ".moeka-a"
    root.mkdir()
    (root / "SOUL.md").write_text("mine\n")
    before = tree(h.home)
    r = h.moeka("new", "a")
    assert r.returncode == 1
    assert tree(h.home) == before


@pytest.mark.fr("FR-013")
def test_empty_existing_target_allowed(h):
    (h.home / ".moeka-a").mkdir()
    r = h.moeka("new", "a")
    assert r.returncode == 0, r.stderr
    assert (h.home / ".moeka-a" / "config.json").exists()


@pytest.mark.fr("FR-013", "FR-014")
def test_custom_workspace_is_registered(h):
    target = h.aux / "custom-ws"
    r = h.moeka("new", "cust", "--workspace", str(target))
    assert r.returncode == 0, (r.stdout, r.stderr)
    reg = h.home / ".config" / "moeka" / "instances"
    assert reg.exists()
    lines = [ln.strip() for ln in reg.read_text().splitlines() if ln.strip()]
    assert str(target) in lines
    assert all(Path(ln).is_absolute() for ln in lines)
    assert load(target)["agents"]["defaults"]["workspace"] == str(target)
    assert not (h.home / ".moeka-cust").exists()


@pytest.mark.fr("FR-014")
def test_workspace_is_absolute_not_placeholder(h):
    assert h.moeka("new", "a").returncode == 0
    ws = load(h.home / ".moeka-a")["agents"]["defaults"]["workspace"]
    assert "${" not in ws and Path(ws).is_absolute()
    assert "${MOEKA_WORKSPACE}" not in (h.home / ".moeka-a" / "config.json").read_text()


# -- FR-015 ports + socket ------------------------------------------------------------------------


@pytest.mark.fr("FR-015")
def test_default_layout_unix_socket(h):
    r = h.moeka("new", "a")
    assert r.returncode == 0
    root = h.home / ".moeka-a"
    cfg = load(root)
    b = cfg["gateway"]["port"]
    assert cfg["api"]["port"] == b + 1
    assert ws_of(cfg).get("unixSocketPath") == str(root / "run" / "websocket.sock")
    run = root / "run"
    assert run.is_dir() and stat.S_IMODE(run.stat().st_mode) == 0o700


@pytest.mark.fr("FR-015")
def test_ws_tcp_layout(h):
    r = h.moeka("new", "a", "--ws-tcp")
    assert r.returncode == 0, r.stderr
    cfg = load(h.home / ".moeka-a")
    b = cfg["gateway"]["port"]
    ws = ws_of(cfg)
    assert cfg["api"]["port"] == b + 1
    assert ws.get("host") == "127.0.0.1" and ws.get("port") == b + 2
    assert not ws.get("unixSocketPath")


# -- FR-016 auto selection ------------------------------------------------------------------------


@pytest.mark.fr("FR-016")
def test_auto_base_is_smallest_free(h):
    exp = expected_base(set())
    r = h.moeka("new", "a")
    assert r.returncode == 0
    assert load(h.home / ".moeka-a")["gateway"]["port"] == exp


@pytest.mark.fr("FR-016")
def test_auto_skips_ports_of_discovered_instances(h):
    first = expected_base(set())
    # default instance occupies the first free base's gateway port
    write_config(h.home / ".nanobot", gateway=first, api=first + 5, ws_socket=True)
    used = {first, first + 5}
    exp = expected_base(used)
    r = h.moeka("new", "a")
    assert r.returncode == 0
    b = load(h.home / ".moeka-a")["gateway"]["port"]
    assert b == exp and b != first


@pytest.mark.fr("FR-016")
def test_auto_skips_registered_instance_api_and_ws_ports(h):
    first = expected_base(set(), ws_tcp=True)
    custom = h.aux / "regp"
    write_config(custom, gateway=40001, api=first + 1, ws_socket=False, ws_port=40002)
    reg = h.home / ".config" / "moeka" / "instances"
    reg.parent.mkdir(parents=True)
    reg.write_text(f"{custom}\n")
    used = {40001, first + 1, 40002}
    exp = expected_base(used, ws_tcp=True)
    r = h.moeka("new", "a", "--ws-tcp")
    assert r.returncode == 0, r.stderr
    b = load(h.home / ".moeka-a")["gateway"]["port"]
    assert b == exp and b != first


@pytest.mark.fr("FR-016")
def test_auto_skips_bound_port(h):
    first = expected_base(set())
    with socket.socket() as s:
        s.bind(("127.0.0.1", first))
        s.listen()
        exp = expected_base(set())
        r = h.moeka("new", "a")
    assert r.returncode == 0
    b = load(h.home / ".moeka-a")["gateway"]["port"]
    assert b != first and b == exp


@pytest.mark.fr("FR-016")
def test_no_free_port_base(h):
    # 34 instances, each burning three distinct bases (gateway, api, websocket TCP).
    for i in range(34):
        root = h.home / f".moeka-f{i:02d}"
        bases = [AUTO_RANGE[(3 * i + k) % len(AUTO_RANGE)] for k in range(3)]
        write_config(root, gateway=bases[0], api=bases[1] + 1, ws_socket=False,
                     ws_port=bases[2])
    r = h.moeka("new", "zz")
    assert r.returncode == 1
    assert "no free port base" in r.stdout + r.stderr
    assert not (h.home / ".moeka-zz" / "config.json").exists()


@pytest.mark.fr("FR-016", "FR-017")
def test_schema_default_ports_count_as_used(h):
    d = h.home / ".nanobot"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(d)}}}))
    r = h.moeka("new", "a", "--port-base", "18789")  # needs 18789,18790 (default gateway)
    assert r.returncode == 1 and "18790" in r.stdout + r.stderr
    r = h.moeka("new", "b", "--port-base", "8899")  # needs 8899,8900 (default api)
    assert r.returncode == 1 and "8900" in r.stdout + r.stderr
    r = h.moeka("new", "c", "--port-base", "8763", "--ws-tcp")  # 8763..8765 (default ws)
    assert r.returncode == 1 and "8765" in r.stdout + r.stderr


# -- FR-017 explicit base -------------------------------------------------------------------------


@pytest.mark.fr("FR-017")
@pytest.mark.parametrize("value", ["1023", "65534", "70000", "abc", "-5", "18800.5", ""])
def test_bad_port_base_exit_2(h, value):
    before = tree(h.home)
    r = h.moeka("new", "a", "--port-base", value)
    assert r.returncode == 2, (value, r.returncode)
    assert tree(h.home) == before


@pytest.mark.fr("FR-017")
def test_explicit_port_base_used(h):
    base = next(p for p in range(41000, 42000, 7) if port_bindable(p) and port_bindable(p + 1))
    r = h.moeka("new", "a", "--port-base", str(base))
    assert r.returncode == 0
    cfg = load(h.home / ".moeka-a")
    assert cfg["gateway"]["port"] == base and cfg["api"]["port"] == base + 1


@pytest.mark.fr("FR-017")
def test_explicit_port_base_conflicts_with_instance(h):
    other = h.home / ".moeka-x"
    write_config(other, gateway=42011, api=42050)
    r = h.moeka("new", "y", "--port-base", "42010")
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "42011" in out and str(other) in out
    assert not (h.home / ".moeka-y" / "config.json").exists()


@pytest.mark.fr("FR-017")
def test_explicit_port_base_bound_by_unrelated_process(h):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        if port > 65533:
            pytest.skip("ephemeral port out of range")
        r = h.moeka("new", "a", "--port-base", str(port))
    assert r.returncode == 1 and str(port) in r.stdout + r.stderr


# -- FR-018 concurrency / SC-007 ------------------------------------------------------------------


@pytest.mark.fr("FR-018")
@pytest.mark.timeout(180)
def test_concurrent_new_never_shares_ports(h):
    names = [f"n{i}" for i in range(8)]
    procs = [h.popen(["bash", str(REPO / "bin" / "moeka.sh"), "new", n]) for n in names]
    for p in procs:
        assert p.wait(timeout=120) == 0
    seen: dict[int, str] = {}
    for n in names:
        cfg = load(h.home / f".moeka-{n}")
        for port in ports_of(cfg):
            assert port not in seen, f"{n} and {seen[port]} share port {port}"
            seen[port] = n


@pytest.mark.fr("SC-007", "FR-016")
@pytest.mark.timeout(180)
def test_sc007_five_consecutive_news(h):
    d = h.home / ".nanobot"
    first = expected_base(set(), ws_tcp=True)
    write_config(d, gateway=first, api=first + 1, ws_socket=False, ws_port=first + 2)
    seen = {first: "default", first + 1: "default", first + 2: "default"}
    for i in range(5):
        t0 = time.monotonic()
        r = h.moeka("new", f"s{i}", *(["--ws-tcp"] if i % 2 else []))
        assert time.monotonic() - t0 < 5
        assert r.returncode == 0
        for port in ports_of(load(h.home / f".moeka-s{i}")):
            assert port not in seen
            seen[port] = f"s{i}"


# -- FR-019 socket path length --------------------------------------------------------------------


@pytest.mark.fr("FR-019")
def test_socket_path_too_long(h):
    base = h.aux / ("d" * 40) / ("e" * 40)
    target = base / "inst"
    assert len(str(target / "run" / "websocket.sock")) > 107
    r = h.moeka("new", "lng", "--workspace", str(target))
    assert r.returncode == 2
    out = r.stdout + r.stderr
    assert "unix socket path too long" in out and "--ws-tcp" in out
    assert not target.exists()
    reg = h.home / ".config" / "moeka" / "instances"
    assert not reg.exists() or str(target) not in reg.read_text()
    r = h.moeka("new", "lng", "--workspace", str(target), "--ws-tcp")
    assert r.returncode == 0, r.stderr


# -- FR-020 keys.env + output ---------------------------------------------------------------------


@pytest.mark.fr("FR-020")
def test_keys_env_mode_and_printed_ports(h):
    r = h.moeka("new", "a")
    assert r.returncode == 0
    root = h.home / ".moeka-a"
    keys = root / "keys.env"
    assert keys.exists() and stat.S_IMODE(keys.stat().st_mode) == 0o600
    example = REPO / "keys.env.example"
    if example.exists():
        assert keys.read_text() == example.read_text()
    cfg = load(root)
    out = r.stdout + r.stderr
    assert str(cfg["gateway"]["port"]) in out and str(cfg["api"]["port"]) in out
    assert str(root / "run" / "websocket.sock") in out
