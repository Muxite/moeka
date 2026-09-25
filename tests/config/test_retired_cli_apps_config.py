"""Old configs that still carry the retired ``tools.cliApps`` key keep loading."""

from __future__ import annotations

import json

from nanobot.config.loader import load_config
from nanobot.config.schema import ToolsConfig


def test_legacy_cli_apps_tools_key_is_ignored(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"cliApps": {"enable": True, "runTimeout": 5}, "my": {}}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert not hasattr(config.tools, "cli_apps")
    assert not hasattr(config.tools, "cliApps")


def test_legacy_cli_apps_snake_case_key_is_ignored():
    tools = ToolsConfig.model_validate({"cli_apps": {"enable": True}})

    assert not hasattr(tools, "cli_apps")
