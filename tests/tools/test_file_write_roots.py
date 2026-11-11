"""tools.file.writeRoots: separate write grants; denials name the missing grant (spec 015)."""

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.filesystem import FileToolsConfig, ReadFileTool, WriteFileTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import ToolsConfig


def _ctx(workspace, read=(), write=()):
    cfg = ToolsConfig(
        restrict_to_workspace=True,
        file=FileToolsConfig(
            read_roots=[str(r) for r in read], write_roots=[str(r) for r in write],
        ),
    )
    return ToolContext(config=cfg, workspace=str(workspace), file_state_store=FileStates())


def _dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    rw = tmp_path / "rw"
    rw.mkdir()
    return ws, ro, rw


def test_write_roots_default_empty_and_alias():
    assert FileToolsConfig().write_roots == []
    assert FileToolsConfig.model_validate({"writeRoots": ["/mnt/x"]}).write_roots == ["/mnt/x"]


@pytest.mark.asyncio
async def test_read_root_alone_never_grants_write(tmp_path):
    ws, ro, _ = _dirs(tmp_path)
    result = await WriteFileTool.create(_ctx(ws, read=[ro])).execute(
        path=str(ro / "x.txt"), content="hi",
    )
    assert "Error" in result and not (ro / "x.txt").exists()
    assert "writeRoots" in result  # names the grant that is missing


@pytest.mark.asyncio
async def test_write_root_allows_write_and_read(tmp_path):
    ws, _, rw = _dirs(tmp_path)
    ctx = _ctx(ws, write=[rw])
    result = await WriteFileTool.create(ctx).execute(path=str(rw / "a.txt"), content="hello")
    assert "Error" not in result and (rw / "a.txt").read_text() == "hello"
    assert "hello" in await ReadFileTool.create(ctx).execute(path=str(rw / "a.txt"))


@pytest.mark.asyncio
async def test_read_denial_names_the_missing_read_grant(tmp_path):
    ws, ro, _ = _dirs(tmp_path)
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    result = await ReadFileTool.create(_ctx(ws, read=[ro])).execute(path=str(outside / "f.txt"))
    assert "outside allowed directory" in result  # runner marker preserved
    assert "readRoots" in result


@pytest.mark.asyncio
async def test_floor_still_applies_inside_a_write_root(tmp_path):
    ws, _, rw = _dirs(tmp_path)
    (rw / "keys.env").write_text("SECRET=1")
    result = await WriteFileTool.create(_ctx(ws, write=[rw])).execute(
        path=str(rw / "keys.env"), content="x",
    )
    assert "Error" in result


def test_exec_binds_follow_the_same_roots(tmp_path):
    ws, ro, rw = _dirs(tmp_path)
    tool = ExecTool.create(_ctx(ws, read=[ro], write=[rw]))
    assert ro.resolve() in tool.sandbox_ro_binds
    assert rw.resolve() in tool.sandbox_rw_binds
