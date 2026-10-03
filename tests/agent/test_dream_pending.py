"""Spec 010 FR-001 / SC-002: Dream's skill writes are staged in ``skills-pending/``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.config.schema import Config, DreamConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

SKILL = "---\nname: {name}\ndescription: Rotate the logs.\n---\n\n# {name}\n\nSteps.\n"


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _loop(workspace: Path, tool_calls: list[list[ToolCallRequest]], **kwargs):
    """A real AgentLoop whose fake provider plays *tool_calls*, then answers ``done``."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    store = MemoryStore(workspace)
    store.write_soul("# Soul")
    store.write_memory("# Memory")
    store.append_history("user rotated the logs again, same three steps")
    store.append_history("user rotated the logs a second time")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.supports_tools = True
    provider.generation = MagicMock(max_tokens=4096)
    script = list(tool_calls)
    prompts: list[str] = []

    async def chat_stream_with_retry(**call):
        prompts.append(str(call.get("messages")))
        if script:
            return LLMResponse(content=None, tool_calls=script.pop(0), finish_reason="tool_calls")
        return LLMResponse(content="done", finish_reason="stop")

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=workspace,
        context_window_tokens=32_000, **kwargs,
    )
    return loop, prompts


def _write_skill(name: str, call_id: str = "c1") -> list[ToolCallRequest]:
    return [ToolCallRequest(
        id=call_id, name="write_file",
        arguments={"path": f"skills/{name}/SKILL.md", "content": SKILL.format(name=name)},
    )]


def test_default_config_is_pending() -> None:
    assert DreamConfig().skill_writes == "pending"
    assert Config().agents.defaults.dream.skill_writes == "pending"
    assert DreamConfig.model_validate({"skillWrites": "direct"}).skill_writes == "direct"


async def test_sc002_dream_creates_skill_in_pending(tmp_path: Path) -> None:
    (tmp_path / "skills" / "existing").mkdir(parents=True)
    (tmp_path / "skills" / "existing" / "SKILL.md").write_text(
        SKILL.format(name="existing"), encoding="utf-8",
    )
    before = _tree(tmp_path / "skills")
    loop, prompts = _loop(tmp_path, [_write_skill("log-rotation")])

    result = await loop.run_dream()

    assert result.status == "completed"
    assert _tree(tmp_path / "skills") == before
    assert [p.name for p in (tmp_path / "skills-pending").iterdir()] == ["log-rotation"]
    staged = tmp_path / "skills-pending" / "log-rotation" / "SKILL.md"
    assert staged.read_text(encoding="utf-8") == SKILL.format(name="log-rotation")
    assert "skills-pending/<name>/" in prompts[0]  # the Dream prompt says so
    assert "log-rotation" not in {s["name"] for s in loop.context.skills.list_skills()}


async def test_dream_merge_edits_a_pending_copy(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "deploy" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(SKILL.format(name="deploy"), encoding="utf-8")
    (skill.parent / "notes.md").write_text("extra\n", encoding="utf-8")
    before = _tree(tmp_path / "skills")
    edit = [ToolCallRequest(id="e1", name="edit_file", arguments={
        "path": "skills/deploy/SKILL.md", "old_text": "Steps.", "new_text": "Steps, now four.",
    })]
    loop, _ = _loop(tmp_path, [edit])

    assert (await loop.run_dream()).status == "completed"

    assert _tree(tmp_path / "skills") == before
    staged = tmp_path / "skills-pending" / "deploy"
    assert "Steps, now four." in (staged / "SKILL.md").read_text(encoding="utf-8")
    assert (staged / "notes.md").read_text(encoding="utf-8") == "extra\n"  # whole skill copied


async def test_dream_reads_its_staged_copy(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    tools = store.build_dream_tools()
    await tools.execute("write_file", {
        "path": "skills/demo/SKILL.md", "content": SKILL.format(name="demo"),
    })
    read = await tools.execute("read_file", {"path": "skills/demo/SKILL.md"})
    assert "Rotate the logs." in str(read)
    patched = await tools.execute("apply_patch", {"edits": [{
        "path": "skills/demo/SKILL.md", "action": "replace",
        "old_text": "Steps.", "new_text": "Patched steps.",
    }]})
    assert "Patch applied" in patched
    assert "Patched steps." in (tmp_path / "skills-pending/demo/SKILL.md").read_text("utf-8")
    assert not (tmp_path / "skills" / "demo").exists()


@pytest.mark.parametrize("tool", ["write_file", "edit_file", "apply_patch"])
async def test_write_inside_skills_pending_lands_exactly_there(tmp_path: Path, tool: str) -> None:
    store = MemoryStore(tmp_path)
    tools = store.build_dream_tools()
    text = SKILL.format(name="demo")
    args = {
        "write_file": {"path": "skills-pending/demo/SKILL.md", "content": text},
        "edit_file": {"path": "skills-pending/demo/SKILL.md", "old_text": "", "new_text": text},
        "apply_patch": {"edits": [{
            "path": "skills-pending/demo/SKILL.md", "action": "add", "new_text": text,
        }]},
    }[tool]
    result = await tools.execute(tool, args)
    target = tmp_path / "skills-pending" / "demo" / "SKILL.md"
    assert target.read_text(encoding="utf-8") == text, result
    assert not (tmp_path / "skills-pending" / "skills-pending").exists()
    assert not (tmp_path / "skills" / "demo").exists()
    assert sorted(p.name for p in (tmp_path / "skills-pending").iterdir()) == ["demo"]


async def test_memory_files_are_not_remapped(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_soul("# Soul\n- Helpful")
    tools = store.build_dream_tools()
    result = await tools.execute("edit_file", {
        "path": "SOUL.md", "old_text": "Helpful", "new_text": "Precise",
    })
    assert "Precise" in store.soul_file.read_text(encoding="utf-8"), result
    assert not (tmp_path / "skills-pending").exists()


async def test_direct_mode_writes_skills(tmp_path: Path) -> None:
    loop, prompts = _loop(tmp_path, [_write_skill("log-rotation")], dream_skill_writes="direct")

    assert (await loop.run_dream()).status == "completed"

    assert (tmp_path / "skills" / "log-rotation" / "SKILL.md").is_file()
    assert not (tmp_path / "skills-pending").exists()
    assert "skills-pending" not in prompts[0]


def test_from_config_passes_skill_writes(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.registry import ToolRegistry

    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.agents.defaults.dream.skill_writes = "direct"
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop.from_config(config, provider=provider, tool_registry=ToolRegistry())
    assert loop.context.memory.dream_skill_writes == "direct"


@pytest.mark.parametrize("mode,expected", [("pending", True), ("direct", False)])
def test_prompt_mentions_staging_only_when_pending(mode: str, expected: bool) -> None:
    assert ("skills-pending/<name>/" in MemoryStore.default_dream_prompt(mode)) is expected
