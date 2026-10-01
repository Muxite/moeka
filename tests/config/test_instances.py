"""Spec 005: host-side instance discovery, status and port assignment (nanobot/config/instances.py)."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.config import instances as inst


def _config(root: Path, **sections) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(sections), encoding="utf-8")
    return root


def test_kind_name_and_unit(tmp_path: Path) -> None:
    home = tmp_path
    assert inst.instance_kind(home / ".nanobot", home) == "default"
    assert inst.unit_for(home / ".nanobot", home) == "moeka.service"
    assert inst.instance_kind(home / ".moeka-a", home) == "named"
    assert inst.instance_name(home / ".moeka-a", home) == "a"
    assert inst.unit_for(home / ".moeka-a", home) == "moeka@a.service"
    for root in (tmp_path / "elsewhere" / "ws", home / ".moeka-Bad", home / "x" / ".moeka-a"):
        assert inst.instance_kind(root, home) == "registered"
        assert inst.unit_for(root, home) is None


def test_name_pattern() -> None:
    ok = ["a", "0", "a-b_c", "a" * 32]
    bad = ["", "A", "a/b", "-a", "_a", "a" * 33, "a.b"]
    assert all(inst.NAME_RE.match(n) for n in ok)
    assert not any(inst.NAME_RE.match(n) for n in bad)


def test_discovery_default_named_registered(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert inst.discover_roots(home) == []
    (home / ".nanobot").mkdir()
    assert inst.discover_roots(home) == []  # no config.json, not discovered
    _config(home / ".nanobot")
    _config(home / ".moeka-b")
    _config(home / ".moeka-a")
    (home / ".moeka-c").mkdir()  # no config
    (home / ".moeka-a-sessions").mkdir()
    custom = _config(tmp_path / "custom" / "ws")
    missing = tmp_path / "gone"
    inst.register(custom, home)
    inst.register(custom, home)  # once only
    inst.register(missing, home)
    roots = inst.discover_roots(home)
    assert roots == [home / ".nanobot", home / ".moeka-a", home / ".moeka-b", custom]
    assert inst.registry_path(home).read_text().splitlines() == [str(custom), str(missing)]


def test_config_ports_defaults_and_socket() -> None:
    ports, sock = inst.config_ports({})
    assert ports == {"gateway": 18790, "api": 8900, "websocket": 8765}
    assert sock is None
    ports, sock = inst.config_ports({
        "gateway": {"port": 1}, "api": {"port": 2},
        "channels": {"websocket": {"unixSocketPath": "/x/run/websocket.sock", "port": 3}},
    })
    assert ports == {"gateway": 1, "api": 2, "websocket": None}
    assert sock == "/x/run/websocket.sock"


def test_choose_port_base_skips_used_and_unbindable(tmp_path: Path) -> None:
    roots = [_config(tmp_path / "r1", gateway={"port": 18800})]
    assert inst.choose_port_base(roots) != 18800
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        busy = holder.getsockname()[1]
        holder.listen(1)
        with pytest.raises(inst.InstanceError) as info:
            inst.choose_port_base([], port_base=busy if busy <= 65533 else 1024)
        if busy <= 65533:
            assert info.value.code == 1 and str(busy) in str(info.value)
    finally:
        holder.close()


def test_explicit_port_base_rules(tmp_path: Path) -> None:
    other = _config(tmp_path / "other", api={"port": 25001})
    with pytest.raises(inst.InstanceError) as info:
        inst.choose_port_base([other], port_base=25000)
    assert info.value.code == 1
    assert "25001" in str(info.value) and str(other) in str(info.value)
    for bad in (1023, 65534):
        with pytest.raises(inst.InstanceError) as info:
            inst.choose_port_base([], port_base=bad)
        assert info.value.code == 2


def test_no_free_port_base(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(inst, "PORT_BASES", (18800,))
    roots = [_config(tmp_path / "r", gateway={"port": 18801})]
    with pytest.raises(inst.InstanceError, match="no free port base"):
        inst.choose_port_base(roots)


def test_run_dir_resolution(tmp_path: Path) -> None:
    explicit = tmp_path / "rd"
    assert inst.run_dir({"MOEKA_RUN_DIR": str(explicit)}) == explicit
    assert stat.S_IMODE(explicit.stat().st_mode) == 0o700
    xdg = tmp_path / "xdg"
    assert inst.run_dir({"XDG_RUNTIME_DIR": str(xdg)}) == xdg / "moeka"
    assert inst.run_dir({}, create=False) == Path(f"/tmp/moeka-{os.getuid()}")


def test_helper_runs_by_path_without_the_venv(tmp_path: Path) -> None:
    """``moeka.sh`` runs the file with ``python3 -P``: stdlib only, no nanobot import."""
    home = tmp_path / "home"
    _config(home / ".moeka-z", gateway={"port": 18900})
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, "-P", "-S", str(Path(inst.__file__)), "list", "--json"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    [item] = json.loads(result.stdout)
    assert item["name"] == "z" and item["ports"]["gateway"] == 18900
    source = Path(inst.__file__).read_text()
    assert "import nanobot" not in source and "from nanobot" not in source
