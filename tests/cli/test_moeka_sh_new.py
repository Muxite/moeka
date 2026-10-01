"""Spec 005 FR-013..FR-020: ``moeka.sh new`` creates instances with their own ports."""

from __future__ import annotations

import concurrent.futures
import json
import socket
import stat
from pathlib import Path

import pytest

from tests._multi_instance import Harness


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    harness.cleanup()


def _ports(config: dict) -> set[int]:
    ports = {config["gateway"]["port"], config["api"]["port"]}
    ws = config.get("channels", {}).get("websocket", {})
    if not ws.get("unixSocketPath"):
        ports.add(ws.get("port", 8765))
    return ports


def test_new_writes_absolute_workspace_ports_socket_and_keys(h: Harness) -> None:
    result = h.run("new", "a")
    assert result.returncode == 0, result.stderr
    root = h.home / ".moeka-a"
    config = h.config(root)
    assert config["agents"]["defaults"]["workspace"] == str(root)
    base = config["gateway"]["port"]
    assert base in range(18800, 19791, 10)
    assert config["api"]["port"] == base + 1
    sock = config["channels"]["websocket"]["unixSocketPath"]
    assert sock == str(root / "run" / "websocket.sock")
    assert stat.S_IMODE((root / "run").stat().st_mode) == 0o700
    keys = root / "keys.env"
    assert keys.is_file() and stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert str(base) in result.stdout and sock in result.stdout
    assert "${" not in (root / "config.json").read_text().split('"workspace"')[1].split(",")[0]


def test_two_instances_get_disjoint_ports_and_avoid_the_default(h: Harness) -> None:
    default = h.home / ".nanobot"
    default.mkdir()
    (default / "config.json").write_text(json.dumps({"gateway": {"port": 18800}}))
    a, b = h.new("a"), h.new("b")
    pa, pb = _ports(h.config(a)), _ports(h.config(b))
    assert not pa & pb
    assert not (pa | pb) & {18800, 8900, 8765}
    assert h.config(a)["channels"]["websocket"]["unixSocketPath"] != \
        h.config(b)["channels"]["websocket"]["unixSocketPath"]


def test_ws_tcp_uses_base_plus_two(h: Harness) -> None:
    root = h.new("t", "--ws-tcp")
    config = h.config(root)
    base = config["gateway"]["port"]
    ws = config["channels"]["websocket"]
    assert ws["host"] == "127.0.0.1" and ws["port"] == base + 2
    assert ws.get("unixSocketPath", "") == ""


@pytest.mark.parametrize("name", ["A", "a/b", "-a", "a" * 33, "a.b"])
def test_bad_names_exit_2(h: Harness, name: str) -> None:
    result = h.run("new", name)
    assert result.returncode == 2, result.stderr
    assert not any(p.name.startswith(".moeka-") for p in h.home.iterdir())


def test_non_empty_target_exits_1_and_writes_nothing(h: Harness) -> None:
    root = h.home / ".moeka-x"
    root.mkdir()
    (root / "keep.txt").write_text("mine")
    result = h.run("new", "x")
    assert result.returncode == 1
    assert sorted(p.name for p in root.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("base", ["1000", "65534", "abc", "-5"])
def test_bad_port_base_exits_2(h: Harness, base: str) -> None:
    result = h.run("new", "p", "--port-base", base)
    assert result.returncode == 2, result.stderr
    assert not (h.home / ".moeka-p").exists()


def test_port_base_taken_by_an_instance_exits_1_naming_it(h: Harness) -> None:
    a = h.new("a")
    taken = h.config(a)["api"]["port"]
    result = h.run("new", "b", "--port-base", str(taken))
    assert result.returncode == 1
    assert str(taken) in result.stderr and str(a) in result.stderr


def test_port_bound_by_another_process(h: Harness) -> None:
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        if port > 65533:
            pytest.skip("ephemeral port above the allowed base range")
        explicit = h.run("new", "e", "--port-base", str(port))
        assert explicit.returncode == 1 and str(port) in explicit.stderr
    finally:
        holder.close()


def test_auto_selection_skips_a_bound_port(h: Harness) -> None:
    holder = socket.socket()
    try:
        try:
            holder.bind(("127.0.0.1", 18800))
        except OSError:
            pytest.skip("18800 already busy on this host")
        holder.listen(1)
        root = h.new("s")
        assert h.config(root)["gateway"]["port"] != 18800
    finally:
        holder.close()


def test_registered_instance_at_custom_path(h: Harness, tmp_path: Path) -> None:
    custom = tmp_path / "srv" / "box"
    result = h.run("new", "box", "--workspace", str(custom))
    assert result.returncode == 0, result.stderr
    assert h.config(custom)["agents"]["defaults"]["workspace"] == str(custom)
    registry = h.home / ".config" / "moeka" / "instances"
    assert registry.read_text().splitlines() == [str(custom)]
    listed = json.loads(h.run("list", "--json").stdout)
    assert [i["workspace"] for i in listed] == [str(custom)]
    assert listed[0]["unit"] is None


def test_socket_path_too_long_exits_2_and_creates_nothing(h: Harness, tmp_path: Path) -> None:
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    result = h.run("new", "long", "--workspace", str(deep))
    assert result.returncode == 2
    assert "unix socket path too long" in result.stderr and "--ws-tcp" in result.stderr
    assert not deep.exists()
    ok = h.run("new", "long", "--workspace", str(deep), "--ws-tcp")
    assert ok.returncode == 0, ok.stderr


def test_concurrent_new_runs_never_share_ports(h: Harness) -> None:
    names = [f"c{i}" for i in range(5)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda n: h.run("new", n), names))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    sets = [_ports(h.config(h.home / f".moeka-{n}")) for n in names]
    seen: set[int] = set()
    for ports in sets:
        assert not ports & seen
        seen |= ports


def test_unexpanded_workspace_refused(h: Harness) -> None:
    env = h.env(MOEKA_WORKSPACE="${MOEKA_WORKSPACE}")
    before = sorted(p.name for p in h.root.iterdir())
    result = h.run("new", "z", env=env)
    assert result.returncode == 2
    assert "MOEKA_WORKSPACE" in result.stderr
    assert sorted(p.name for p in h.root.iterdir()) == before
    assert not (h.home / ".moeka-z").exists()
