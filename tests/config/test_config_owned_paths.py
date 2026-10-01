"""Spec 005 FR-027..FR-031: config-owned paths instead of process globals."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from nanobot.config import loader
from nanobot.config.loader import (
    ConfigPathConflictError,
    load_config,
    pin_config_path,
    reset_config_path,
    set_config_path,
)
from nanobot.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_data_dir,
    get_legacy_sessions_dir,
    get_state_home,
)
from nanobot.gateway.runtime import GatewayInstance
from nanobot.gateway.service import (
    GatewayServiceInstaller,
    GatewayServiceOptions,
    GatewayStartOptions,
)
from nanobot.kernel.legacy import LegacyEnvironment


@pytest.fixture(autouse=True)
def _clean_config_path():
    reset_config_path()
    yield
    reset_config_path()


def _config_file(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    path.write_text('{"agents": {"defaults": {"workspace": "%s"}}}' % (root / "ws"))
    return path


def test_legacy_environment_uses_the_configs_own_directory(tmp_path: Path) -> None:
    a = _config_file(tmp_path / "A")
    b = _config_file(tmp_path / "B")
    set_config_path(a)
    config = load_config(b)
    paths = LegacyEnvironment.from_config(config).paths
    assert paths.data_dir == b.parent.resolve()
    assert paths.media_dir == (b.parent / "media").resolve()
    assert paths.logs_dir == (b.parent / "logs").resolve()


def test_gateway_default_identity_follows_the_state_home(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "inst"
    monkeypatch.setenv("MOEKA_WORKSPACE", str(root))
    assert get_state_home() == root
    instance = GatewayInstance.resolve(config_path=root / "config.json")
    assert instance.paths.state_path == root / "run" / "gateway.json"
    other = GatewayInstance.resolve(config_path=tmp_path / "other" / "config.json")
    assert other.paths.state_path != tmp_path / "other" / "run" / "gateway.json"


def _launchd(tmp_path: Path, **start) -> dict:
    installer = GatewayServiceInstaller(
        platform_name="Darwin", home=tmp_path / "fakehome", subprocess_run=lambda *a, **k: None,
    )
    result = installer.install(
        GatewayServiceOptions(start=GatewayStartOptions(port=18790, **start)), dry_run=True,
    )
    return plistlib.loads(result.content.encode())


def test_launchd_logs_live_under_the_instance_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOEKA_WORKSPACE", str(tmp_path / "statehome"))
    by_config = _launchd(tmp_path, config_path=str(tmp_path / "cfgdir" / "config.json"))
    assert Path(by_config["StandardOutPath"]).parent == (tmp_path / "cfgdir" / "logs").resolve()
    by_ws = _launchd(tmp_path, workspace=str(tmp_path / "ws"))
    assert Path(by_ws["StandardErrorPath"]).parent == (tmp_path / "ws").resolve() / "logs"
    default = _launchd(tmp_path)
    assert Path(default["StandardOutPath"]).parent == tmp_path / "statehome" / "logs"
    for plist in (by_config, by_ws, default):
        assert ".nanobot" not in plist["StandardOutPath"]


def test_path_helpers_take_a_data_dir_or_the_active_config(tmp_path: Path) -> None:
    d = tmp_path / "data"
    assert get_cli_history_path(d) == d / "history" / "cli_history"
    assert get_bridge_install_dir(d) == d / "bridge"
    assert get_legacy_sessions_dir(d) == d / "sessions"
    set_config_path(tmp_path / "inst" / "config.json")
    assert get_data_dir() == tmp_path / "inst"
    assert get_cli_history_path() == tmp_path / "inst" / "history" / "cli_history"
    assert get_bridge_install_dir() == tmp_path / "inst" / "bridge"
    assert get_legacy_sessions_dir() == tmp_path / "inst" / "sessions"


def test_pin_config_path(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "config.json", tmp_path / "b" / "config.json"
    pin_config_path(a)
    assert loader.get_config_path() == a.resolve()
    pin_config_path(a)  # same path: no-op
    set_config_path(tmp_path / "a" / ".." / "a" / "config.json")  # same resolved path
    with pytest.raises(ConfigPathConflictError):
        set_config_path(b)
    with pytest.raises(ConfigPathConflictError):
        pin_config_path(b)
    assert issubclass(ConfigPathConflictError, RuntimeError)
    reset_config_path()
    set_config_path(b)
    assert loader.get_config_path() == b


def test_gateway_command_pins_its_config(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from nanobot.cli import commands

    seen: list[Path] = []

    def fake_run_gateway(config, **kwargs):
        seen.append(loader._pinned_config_path)
        with pytest.raises(ConfigPathConflictError):
            set_config_path(tmp_path / "elsewhere" / "config.json")

    monkeypatch.setattr("nanobot.cli.gateway_runtime._run_gateway", fake_run_gateway)
    cfg = _config_file(tmp_path / "inst")
    app = commands.create_gateway_app(
        console=commands.console, log_handler_id=commands._log_handler_id,
        load_runtime_config=commands._load_runtime_config, run_gateway=fake_run_gateway,
    )
    result = CliRunner().invoke(app, ["--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert seen == [cfg.resolve()]
    assert loader._pinned_config_path is None  # released when the command ended
