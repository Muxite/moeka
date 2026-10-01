"""Group E: config-owned paths instead of process globals (FR-027..FR-032, US8)."""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

import pytest

from conftest import REPO


def _cfg(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "config.json"
    p.write_text(json.dumps({"agents": {"defaults": {"workspace": str(root)}}}))
    return p


@pytest.mark.fr("FR-027")
def test_legacy_env_paths_follow_config_file(h):
    from nanobot.config.loader import load_config, set_config_path
    from nanobot.kernel.legacy import LegacyEnvironment

    a = h.home / "inst-a"
    b = h.home / "inst-b"
    _cfg(a)
    cfg_b = load_config(_cfg(b))
    set_config_path(a / "config.json")
    paths = LegacyEnvironment.from_config(cfg_b).paths
    assert paths.data_dir == b.resolve()
    assert paths.media_dir == (b / "media").resolve()
    assert paths.logs_dir == (b / "logs").resolve()


@pytest.mark.fr("FR-027")
def test_legacy_env_paths_bound_before_global_changes(h):
    from nanobot.config.loader import load_config, set_config_path
    from nanobot.kernel.legacy import LegacyEnvironment

    a = h.home / "inst-a"
    b = h.home / "inst-b"
    _cfg(a)
    set_config_path(a / "config.json")
    cfg_b = load_config(_cfg(b))
    env = LegacyEnvironment.from_config(cfg_b)
    set_config_path(a / "config.json")
    assert env.paths.data_dir == b.resolve()
    assert env.paths.logs_dir == (b / "logs").resolve()


@pytest.mark.fr("FR-028")
def test_gateway_default_identity_from_state_home(h, monkeypatch):
    from nanobot.gateway.runtime import GatewayInstance

    x = h.home / "inst-x"
    _cfg(x)
    monkeypatch.setenv("MOEKA_WORKSPACE", str(x))
    inst = GatewayInstance.resolve(config_path=x / "config.json")
    assert inst.paths.state_path == x.resolve() / "run" / "gateway.json"
    # ~/.nanobot is no longer the default identity when MOEKA_WORKSPACE points elsewhere.
    legacy = h.home / ".nanobot"
    _cfg(legacy)
    other = GatewayInstance.resolve(config_path=legacy / "config.json")
    assert other.paths.state_path != legacy.resolve() / "run" / "gateway.json"


def _launchd_content(h, **start_kw) -> dict:
    from nanobot.gateway import GatewayStartOptions
    from nanobot.gateway.service import GatewayServiceInstaller, GatewayServiceOptions

    calls = []
    inst = GatewayServiceInstaller(platform_name="Darwin", home=h.home,
                                   subprocess_run=lambda *a, **k: calls.append(a))
    result = inst.install(GatewayServiceOptions(start=GatewayStartOptions(port=18999,
                                                                          **start_kw)),
                          dry_run=True)
    assert not calls
    return plistlib.loads(result.content.encode())


@pytest.mark.fr("FR-029")
def test_launchd_logs_under_config_dir(h):
    c = h.home / "inst-c"
    _cfg(c)
    plist = _launchd_content(h, config_path=str(c / "config.json"))
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(plist[key]).parent == c.resolve() / "logs", plist[key]


@pytest.mark.fr("FR-029")
def test_launchd_logs_under_workspace(h):
    w = h.home / "inst-w"
    w.mkdir()
    plist = _launchd_content(h, workspace=str(w))
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(plist[key]).parent == w.resolve() / "logs"


@pytest.mark.fr("FR-029")
def test_launchd_logs_under_state_home(h, monkeypatch):
    s = h.home / "inst-s"
    s.mkdir()
    monkeypatch.setenv("MOEKA_WORKSPACE", str(s))
    plist = _launchd_content(h)
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(plist[key]).parent == s.resolve() / "logs"
        assert ".nanobot" not in plist[key]


@pytest.mark.fr("FR-030")
def test_path_helpers_accept_data_dir(h):
    from nanobot.config.paths import (
        get_bridge_install_dir,
        get_cli_history_path,
        get_legacy_sessions_dir,
    )

    d = h.home / "dd"
    assert get_cli_history_path(data_dir=d) == d / "history" / "cli_history"
    assert get_bridge_install_dir(data_dir=d) == d / "bridge"
    assert get_legacy_sessions_dir(data_dir=d) == d / "sessions"


@pytest.mark.fr("FR-030")
def test_path_helpers_default_to_config_dir(h, monkeypatch):
    from nanobot.config.loader import set_config_path
    from nanobot.config.paths import (
        get_bridge_install_dir,
        get_cli_history_path,
        get_data_dir,
        get_legacy_sessions_dir,
    )

    c = h.home / "inst-c"
    _cfg(c)
    monkeypatch.setenv("MOEKA_WORKSPACE", str(h.home / "state-home-elsewhere"))
    set_config_path(c / "config.json")
    data = get_data_dir()
    assert Path(data).resolve() == c.resolve()
    assert Path(get_cli_history_path()).resolve() == (c / "history" / "cli_history").resolve()
    assert Path(get_bridge_install_dir()).resolve() == (c / "bridge").resolve()
    assert Path(get_legacy_sessions_dir()).resolve() == (c / "sessions").resolve()


@pytest.mark.fr("FR-031")
def test_pin_config_path(h):
    from nanobot.config.loader import (
        ConfigPathConflictError,
        get_config_path,
        pin_config_path,
        reset_config_path,
        set_config_path,
    )

    assert issubclass(ConfigPathConflictError, RuntimeError)
    a = _cfg(h.home / "pa")
    b = _cfg(h.home / "pb")
    pin_config_path(a)
    assert Path(get_config_path()).resolve() == a.resolve()
    with pytest.raises(ConfigPathConflictError):
        set_config_path(b)
    with pytest.raises(ConfigPathConflictError):
        pin_config_path(b)
    assert Path(get_config_path()).resolve() == a.resolve()
    set_config_path(a)  # same path: no-op
    pin_config_path(h.home / "pa" / ".." / "pa" / "config.json")  # same resolved path
    link = h.home / "pa-link"
    link.symlink_to(h.home / "pa")
    set_config_path(link / "config.json")
    reset_config_path()
    set_config_path(b)
    assert Path(get_config_path()).resolve() == b.resolve()


@pytest.mark.fr("FR-032")
def test_doc_multiple_instances_rules():
    doc = REPO / "docs" / "multiple-instances.md"
    text = doc.read_text()
    low = re.sub(r"[`*_]", "", text.lower())
    assert "config path" in low or "set_config_path" in text or "_current_config_path" in text
    assert re.search(r"one kernel per state dir", low)
    assert re.search(r"many kernels (may|can) share (a|one) data dir", low)
    assert "memory_key" in text
    assert "kernel-native" in low or "kernel native" in low


@pytest.mark.fr("FR-050")
def test_capbudget_docstring_says_in_memory_per_process():
    from moeka.budget import CapBudget

    doc = (CapBudget.__doc__ or "").lower()
    assert "in-memory" in doc or "in memory" in doc
    assert "per process" in doc or "per-process" in doc
