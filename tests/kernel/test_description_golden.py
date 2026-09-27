"""Golden snapshot of every built-in tool's ``description`` and ``parameters`` (Task 20).

- The fixture ``tests/kernel/fixtures/tool_description_golden.json`` was generated from the
  live ``Tool`` classes BEFORE any description moved into a data file. It is the
  regression tripwire for "descriptions as data": whatever the source of a string
  (Python literal or ``nanobot/agent/tools/descriptions/*.txt``), what the model sees
  must stay byte-identical.
- Descriptions compare with ``==`` on the exact string.
- Parameters compare as serialized JSON WITHOUT key sorting, so key order (which the
  provider sees) is pinned too, not just dict equality.
- ``ExecTool`` has a platform branch; the snapshot is the POSIX variant (the test
  image is Linux). ``my`` is snapshotted in both of its config-dependent modes.
- Regenerate (only when a description change is intended):
  ``python tests/kernel/test_description_golden.py`` from the repo root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

GOLDEN = Path(__file__).parent / "fixtures" / "tool_description_golden.json"


def _slim_ctx(tmp: Path):
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.exec_session import ExecSessionManager
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ToolsConfig
    from nanobot.session.manager import SessionManager

    workspace = tmp / "workspace"
    workspace.mkdir()
    return ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        bus=MessageBus(),
        subagent_manager=SimpleNamespace(get_running_count=lambda: 0, max_concurrent_subagents=4),
        exec_session_manager=ExecSessionManager(),
        sessions=SessionManager(workspace, sessions_root=tmp / "runtime"),
        timezone="UTC",
    )


def collect_tools(tmp: Path) -> dict[str, Any]:
    """Every built-in tool instance, keyed by snapshot name, read from the live classes."""
    from nanobot.agent.tools.bg_shell import BackgroundShellTool
    from nanobot.agent.tools.image_generation import (
        ImageGenerationTool,
        ImageGenerationToolConfig,
    )
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.self import MyTool

    registry = ToolRegistry()
    names = ToolLoader().load(_slim_ctx(tmp), registry)
    tools: dict[str, Any] = {name: registry.get(name) for name in names}
    tools["generate_image"] = ImageGenerationTool(
        workspace=tmp, config=ImageGenerationToolConfig()
    )
    tools["bg_shell"] = BackgroundShellTool(registry=None)  # type: ignore[arg-type]
    tools["my"] = MyTool(runtime_control=SimpleNamespace(), modify_allowed=True)  # type: ignore[arg-type]
    tools["my[read_only]"] = MyTool(runtime_control=SimpleNamespace(), modify_allowed=False)  # type: ignore[arg-type]
    return tools


def snapshot(tmp: Path) -> dict[str, dict[str, Any]]:
    return {
        key: {"description": tool.description, "parameters": tool.parameters}
        for key, tool in sorted(collect_tools(tmp).items())
    }


def _golden() -> dict[str, dict[str, Any]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# A missing fixture fails ``test_golden_covers_exactly_the_live_tool_set``, not collection.
_GOLDEN_KEYS = sorted(_golden()) if GOLDEN.is_file() else []


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict[str, dict[str, Any]]:
    return snapshot(tmp_path_factory.mktemp("golden"))


def test_golden_covers_exactly_the_live_tool_set(live) -> None:
    assert sorted(live) == sorted(_golden())


@pytest.mark.parametrize("key", _GOLDEN_KEYS)
def test_description_is_byte_identical(live, key: str) -> None:
    assert live[key]["description"] == _golden()[key]["description"]


@pytest.mark.parametrize("key", _GOLDEN_KEYS)
def test_parameters_are_identical_including_key_order(live, key: str) -> None:
    expected = json.dumps(_golden()[key]["parameters"], ensure_ascii=False)
    assert json.dumps(live[key]["parameters"], ensure_ascii=False) == expected


def test_schema_sent_to_provider_is_unchanged(live, tmp_path) -> None:
    """``to_schema`` (the provider payload) carries the same description and parameters."""
    for key, tool in collect_tools(tmp_path).items():
        schema = tool.to_schema()["function"]
        assert schema["description"] == live[key]["description"], key
        assert json.dumps(schema["parameters"]) == json.dumps(live[key]["parameters"]), key


if __name__ == "__main__":  # regenerate the fixture: python tests/kernel/test_description_golden.py
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory() as d:
        data = snapshot(Path(d))
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(data)} tools to {GOLDEN}")
