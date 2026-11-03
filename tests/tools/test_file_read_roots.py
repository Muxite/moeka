"""tools.file.readRoots: broad READ-ONLY roots; writes and the protected floor are unchanged."""

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.filesystem import (
    FileToolsConfig,
    ReadFileTool,
    WriteFileTool,
)
from nanobot.config.schema import ToolsConfig


def _ctx(workspace, read_roots):
    cfg = ToolsConfig(
        restrict_to_workspace=True,
        file=FileToolsConfig(read_roots=[str(r) for r in read_roots]),
    )
    return ToolContext(config=cfg, workspace=str(workspace), file_state_store=FileStates())


def test_read_roots_default_empty():
    assert FileToolsConfig().read_roots == []


def test_read_roots_accept_camel_case_alias():
    assert FileToolsConfig.model_validate({"readRoots": ["/mnt"]}).read_roots == ["/mnt"]


@pytest.mark.asyncio
async def test_read_outside_workspace_allowed_under_read_root(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "stagecraft"
    root.mkdir()
    src = root / "Scene.tsx"
    src.write_text("export const Scene = 1", encoding="utf-8")

    tool = ReadFileTool.create(_ctx(workspace, [root]))
    result = await tool.execute(path=str(src))
    assert "export const Scene" in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_read_outside_every_root_still_blocked(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "stagecraft"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("nope", encoding="utf-8")

    tool = ReadFileTool.create(_ctx(workspace, [root]))
    result = await tool.execute(path=str(other / "x.txt"))
    assert "Error" in result
    assert "outside" in result.lower()


@pytest.mark.asyncio
async def test_read_root_does_not_widen_writes(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "stagecraft"
    root.mkdir()

    tool = WriteFileTool.create(_ctx(workspace, [root]))
    result = await tool.execute(path=str(root / "hack.txt"), content="pwned")
    assert "Error" in result
    assert not (root / "hack.txt").exists()


@pytest.mark.asyncio
async def test_protected_keys_env_denied_under_read_root(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "home"
    root.mkdir()
    keys = root / "keys.env"
    keys.write_text("SECRET=1", encoding="utf-8")

    tool = ReadFileTool.create(_ctx(workspace, [root]))
    result = await tool.execute(path=str(keys))
    assert "Error" in result
    assert "SECRET=1" not in result
