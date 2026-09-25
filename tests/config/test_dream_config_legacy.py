"""Old configs that still carry the retired ``dream.cron`` key keep loading."""

from __future__ import annotations

import json

from loguru import logger

from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import DreamConfig


def _write_legacy_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "dream": {
                            "intervalH": 5,
                            "cron": "0 */4 * * *",
                            "modelOverride": None,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_legacy_dream_cron_loads_and_is_dropped_with_warning(tmp_path):
    config_path = _write_legacy_config(tmp_path)
    records: list[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        config = load_config(config_path)
    finally:
        logger.remove(handler_id)

    dream = config.agents.defaults.dream
    assert dream.interval_h == 5
    assert not hasattr(dream, "cron")
    assert "cron" not in dream.model_dump(by_alias=True)
    assert any("dream.cron" in r for r in records), records


def test_legacy_dream_cron_is_not_written_back(tmp_path):
    config_path = _write_legacy_config(tmp_path)

    save_config(load_config(config_path), config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "cron" not in saved["agents"]["defaults"]["dream"]
    assert saved["agents"]["defaults"]["dream"]["intervalH"] == 5


def test_dream_config_has_no_scheduler_api():
    assert not hasattr(DreamConfig, "build_schedule")
    assert not hasattr(DreamConfig, "describe_schedule")
    assert "cron" not in DreamConfig.model_fields
