"""Integration tests for `nanobot channels enable/disable/status`.

These use the Typer CliRunner against a real temporary config.json file —
no mocking of file I/O or subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app

runner = CliRunner()


@pytest.fixture()
def real_config(tmp_path: Path) -> Path:
    """Write a minimal but realistic config.json with multiple channel stubs."""
    cfg = {
        "channels": {
            "telegram": {
                "enabled": False,
                "token": "${TELEGRAM_TOKEN}",
                "allowFrom": ["*"],
            },
            "discord": {
                "enabled": False,
                "token": "${DISCORD_TOKEN}",
                "allowFrom": ["*"],
            },
        }
    }
    f = tmp_path / "config.json"
    f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# channels status
# ---------------------------------------------------------------------------

def test_channels_status_shows_all_known_channels(real_config: Path) -> None:
    result = runner.invoke(app, ["channels", "status", "--config", str(real_config)])
    assert result.exit_code == 0, result.stdout
    # At minimum the two we added must appear
    assert "Telegram" in result.stdout or "telegram" in result.stdout.lower()


def test_channels_status_reflects_enabled_flag(real_config: Path) -> None:
    # Enable telegram first
    data = json.loads(real_config.read_text())
    data["channels"]["telegram"]["enabled"] = True
    real_config.write_text(json.dumps(data, indent=2))

    result = runner.invoke(app, ["channels", "status", "--config", str(real_config)])
    assert result.exit_code == 0, result.stdout
    # The row for Telegram should show a checkmark
    assert "✓" in result.stdout or "yes" in result.stdout.lower()


# ---------------------------------------------------------------------------
# channels enable
# ---------------------------------------------------------------------------

def test_enable_telegram_writes_enabled_true(real_config: Path) -> None:
    result = runner.invoke(
        app, ["channels", "enable", "telegram", "--config", str(real_config)]
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(real_config.read_text())
    assert data["channels"]["telegram"]["enabled"] is True


def test_enable_does_not_corrupt_token_or_allow_from(real_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--config", str(real_config)])
    data = json.loads(real_config.read_text())
    assert data["channels"]["telegram"]["token"] == "${TELEGRAM_TOKEN}"
    assert data["channels"]["telegram"]["allowFrom"] == ["*"]


def test_enable_produces_valid_json_after_write(real_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "discord", "--config", str(real_config)])
    # File must still be valid JSON
    data = json.loads(real_config.read_text())
    assert "channels" in data


def test_enable_can_enable_both_channels_independently(real_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--config", str(real_config)])
    runner.invoke(app, ["channels", "enable", "discord", "--config", str(real_config)])
    data = json.loads(real_config.read_text())
    assert data["channels"]["telegram"]["enabled"] is True
    assert data["channels"]["discord"]["enabled"] is True


# ---------------------------------------------------------------------------
# channels disable
# ---------------------------------------------------------------------------

def test_disable_telegram_writes_enabled_false(real_config: Path) -> None:
    # First enable
    data = json.loads(real_config.read_text())
    data["channels"]["telegram"]["enabled"] = True
    real_config.write_text(json.dumps(data, indent=2))

    result = runner.invoke(
        app, ["channels", "disable", "telegram", "--config", str(real_config)]
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(real_config.read_text())
    assert data["channels"]["telegram"]["enabled"] is False


def test_disable_preserves_other_channel(real_config: Path) -> None:
    # Enable both, then disable only telegram
    data = json.loads(real_config.read_text())
    data["channels"]["telegram"]["enabled"] = True
    data["channels"]["discord"]["enabled"] = True
    real_config.write_text(json.dumps(data, indent=2))

    runner.invoke(app, ["channels", "disable", "telegram", "--config", str(real_config)])
    data = json.loads(real_config.read_text())
    assert data["channels"]["telegram"]["enabled"] is False
    assert data["channels"]["discord"]["enabled"] is True


# ---------------------------------------------------------------------------
# enable → disable → enable cycle
# ---------------------------------------------------------------------------

def test_enable_disable_cycle_is_stable(real_config: Path) -> None:
    """Repeated enable/disable calls converge to the expected final state."""
    for _ in range(3):
        runner.invoke(app, ["channels", "enable", "telegram", "--config", str(real_config)])
        data = json.loads(real_config.read_text())
        assert data["channels"]["telegram"]["enabled"] is True

        runner.invoke(app, ["channels", "disable", "telegram", "--config", str(real_config)])
        data = json.loads(real_config.read_text())
        assert data["channels"]["telegram"]["enabled"] is False


# ---------------------------------------------------------------------------
# error cases
# ---------------------------------------------------------------------------

def test_enable_unknown_channel_exits_nonzero(real_config: Path) -> None:
    result = runner.invoke(
        app, ["channels", "enable", "unknown_xyz", "--config", str(real_config)]
    )
    assert result.exit_code != 0


def test_disable_unknown_channel_exits_nonzero(real_config: Path) -> None:
    result = runner.invoke(
        app, ["channels", "disable", "unknown_xyz", "--config", str(real_config)]
    )
    assert result.exit_code != 0


def test_enable_missing_config_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["channels", "enable", "telegram", "--config", str(tmp_path / "nope.json")],
    )
    assert result.exit_code != 0
