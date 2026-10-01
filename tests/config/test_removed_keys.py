"""``display`` config section and warning hygiene for the consolidated config.

``channels``/``gateway``/``api``/``heartbeat``/``transcription`` are live host sections again
(consolidation, 003); the slim-era "retired section" tests were dropped with them.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.config.loader import load_config
from nanobot.config.schema import Config

def _capture_warnings():
    """Return (records, handler_id) for a loguru WARNING-level sink."""
    from loguru import logger as loguru_logger

    records: list[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    return records, handler_id


def test_config_without_retired_sections_does_not_warn(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agents": {"defaults": {"model": "m"}}}), encoding="utf-8")

    from loguru import logger as loguru_logger

    records, handler_id = _capture_warnings()
    try:
        load_config(path)
    finally:
        loguru_logger.remove(handler_id)

    assert not [r for r in records if "retired" in r.lower()], records


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
