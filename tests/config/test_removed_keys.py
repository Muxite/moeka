"""Legacy chat-runtime config sections load but are ignored after the core slim.

``channels``, ``gateway``, ``api``, ``heartbeat`` and ``transcription`` belonged to
the removed channel/gateway/WebUI runtime. Existing ``config.json`` files keep
loading: pydantic drops the unknown keys, and nothing is migrated (in particular
``channels.showReasoning`` etc. do not flow into ``display``).
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.config.loader import load_config
from nanobot.config.schema import Config

_REMOVED = ("channels", "gateway", "api", "heartbeat", "transcription")


def _write_legacy_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "anthropic/claude-test-model"}},
                "channels": {
                    "showReasoning": False,
                    "sendToolHints": False,
                    "sendProgress": False,
                    "telegram": {"enabled": True, "token": "x"},
                },
                "gateway": {"host": "0.0.0.0", "port": 1234, "restartMode": "exit"},
                "api": {"host": "0.0.0.0", "port": 8900},
                "heartbeat": {"enabled": True, "intervalS": 60},
                "transcription": {"provider": "groq", "language": "en"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_legacy_runtime_sections_load_and_are_dropped(tmp_path: Path) -> None:
    config = load_config(_write_legacy_config(tmp_path))

    assert config.agents.defaults.model == "anthropic/claude-test-model"
    for name in _REMOVED:
        assert name not in Config.model_fields, name
        assert not hasattr(config, name), name


def test_legacy_channel_display_flags_are_not_migrated(tmp_path: Path) -> None:
    config = load_config(_write_legacy_config(tmp_path))

    assert config.display.show_reasoning is True
    assert config.display.send_tool_hints is True
    assert config.display.send_progress is True


def test_display_config_accepts_camel_case(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"display": {"showReasoning": False, "sendToolHints": False}}),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.display.show_reasoning is False
    assert config.display.send_tool_hints is False
    assert config.display.send_progress is True
