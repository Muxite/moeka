"""Resolved-path floor for the file tools (phase-0 P0.1a).

The floor denies a few internal paths to every file tool even when
``restrict_to_workspace`` is off or ``extra_*_allowed_*`` lists cover them,
and it is checked after ``resolve()`` so symlinks do not get around it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.memory import MemoryStore
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from nanobot.agent.tools.image_generation import (
    ImageGenerationTool,
    ImageGenerationToolConfig,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import FindFilesTool, GrepTool
from nanobot.config.schema import ProviderConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.image_generation import ImageGenerationError
from nanobot.session.sqlite_store import default_sessions_root
from nanobot.utils.runtime import repeated_workspace_violation_error

DENIAL = "protected internal path (not configurable)"
SECRET = "sk-floor-test-secret-0123456789"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)

needs_proc = pytest.mark.skipif(
    not Path("/proc/self/environ").exists(), reason="requires Linux /proc"
)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An instance data dir with an OAuth store and plugin data inside it."""
    data = tmp_path / "data"
    (data / "auth").mkdir(parents=True)
    (data / "auth" / "mcp.json").write_text(f'{{"token": "{SECRET}"}}\n', encoding="utf-8")
    (data / "plugin-data" / "abc" / "demo").mkdir(parents=True)
    (data / "plugin-data" / "abc" / "demo" / "state.txt").write_text(SECRET, encoding="utf-8")
    (data / "notes.txt").write_text(f"public {SECRET[:6]} note\n", encoding="utf-8")
    monkeypatch.setattr("nanobot.config.loader._current_config_path", data / "config.json")
    return data


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "history.jsonl").write_text(
        '{"cursor": 1, "content": "hello"}\n', encoding="utf-8",
    )
    return ws


# ---------------------------------------------------------------------------
# /proc secrets
# ---------------------------------------------------------------------------


@needs_proc
async def test_read_proc_environ_denied_when_unrestricted(workspace: Path) -> None:
    tool = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    for path in ("/proc/self/environ", f"/proc/{os.getpid()}/environ", "/proc/thread-self/environ"):
        result = await tool.execute(path=path)
        assert DENIAL in str(result), path
        assert "PATH=" not in str(result)


@needs_proc
async def test_read_proc_mem_maps_and_root_denied(workspace: Path) -> None:
    tool = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    for path in ("/proc/self/maps", "/proc/self/mem", "/proc/self/root/proc/self/environ"):
        assert DENIAL in str(await tool.execute(path=path)), path


@needs_proc
async def test_read_symlink_to_proc_environ_denied(workspace: Path) -> None:
    link = workspace / "e"
    link.symlink_to("/proc/self/environ")
    (workspace / "sub").mkdir()
    tool = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    for path in (str(link), "e", "sub/../e"):
        result = await tool.execute(path=path)
        assert DENIAL in str(result), path
        assert "PATH=" not in str(result)


@needs_proc
async def test_read_symlink_to_proc_environ_denied_when_restricted(workspace: Path) -> None:
    (workspace / "e").symlink_to("/proc/self/environ")
    tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    result = await tool.execute(path="e")
    assert "PATH=" not in str(result)
    assert result.is_error


@needs_proc
async def test_proc_status_cmdline_cpuinfo_meminfo_still_readable(workspace: Path) -> None:
    tool = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    status = await tool.execute(path="/proc/self/status")
    assert DENIAL not in str(status)
    assert "Pid" in str(status)
    for path in ("/proc/self/cmdline", "/proc/cpuinfo", "/proc/meminfo"):
        assert DENIAL not in str(await tool.execute(path=path)), path


# ---------------------------------------------------------------------------
# internal-state files: write denied, read allowed
# ---------------------------------------------------------------------------


async def test_write_history_jsonl_denied_but_read_allowed(workspace: Path) -> None:
    history = workspace / "memory" / "history.jsonl"
    before = history.read_text(encoding="utf-8")

    read = await ReadFileTool(workspace=workspace).execute(path="memory/history.jsonl")
    assert DENIAL not in str(read)
    assert "hello" in str(read)

    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    for path in ("memory/history.jsonl", str(history)):
        result = await write.execute(path=path, content="forged\n")
        assert DENIAL in str(result), path
    assert history.read_text(encoding="utf-8") == before


async def test_write_dream_cursor_and_workspace_id_denied(workspace: Path) -> None:
    (workspace / ".nanobot").mkdir()
    (workspace / ".nanobot" / "workspace-id").write_text("a" * 32 + "\n", encoding="utf-8")
    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    for path in ("memory/.dream_cursor", ".nanobot/workspace-id"):
        assert DENIAL in str(await write.execute(path=path, content="999\n")), path
    assert not (workspace / "memory" / ".dream_cursor").exists()
    assert (workspace / ".nanobot" / "workspace-id").read_text(encoding="utf-8") == "a" * 32 + "\n"


async def test_write_through_symlink_to_history_denied(workspace: Path) -> None:
    history = workspace / "memory" / "history.jsonl"
    before = history.read_text(encoding="utf-8")
    (workspace / "innocent.txt").symlink_to(history)
    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    assert DENIAL in str(await write.execute(path="innocent.txt", content="x\n"))
    assert history.read_text(encoding="utf-8") == before


async def test_apply_patch_and_edit_file_denied_on_history_jsonl(workspace: Path) -> None:
    history = workspace / "memory" / "history.jsonl"
    before = history.read_text(encoding="utf-8")

    edit = EditFileTool(workspace=workspace, restrict_to_workspace=False)
    edit_result = await edit.execute(
        path="memory/history.jsonl", old_text="hello", new_text="forged",
    )
    assert DENIAL in str(edit_result)

    patch = ApplyPatchTool(workspace=workspace, restrict_to_workspace=False)
    for edits in (
        [{"path": "memory/history.jsonl", "action": "add", "new_text": '{"cursor": 2}'}],
        [{
            "path": "memory/history.jsonl", "action": "replace",
            "old_text": "hello", "new_text": "forged",
        }],
    ):
        assert DENIAL in str(await patch.execute(edits=edits))
    assert history.read_text(encoding="utf-8") == before


async def test_memory_store_still_writes_history_and_dream_cursor(workspace: Path) -> None:
    """The write floor governs the file tools only, not MemoryStore's own I/O."""
    store = MemoryStore(workspace)
    cursor = store.append_history("from code")
    store.set_last_dream_cursor(cursor)
    assert "from code" in (workspace / "memory" / "history.jsonl").read_text(encoding="utf-8")
    assert store.get_last_dream_cursor() == cursor


# ---------------------------------------------------------------------------
# data dir: auth/, plugin-data/, sessions root
# ---------------------------------------------------------------------------


async def test_auth_dir_read_and_write_denied(data_dir: Path, workspace: Path) -> None:
    mcp = data_dir / "auth" / "mcp.json"
    before = mcp.read_text(encoding="utf-8")

    read = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    result = await read.execute(path=str(mcp))
    assert DENIAL in str(result)
    assert SECRET not in str(result)

    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    assert DENIAL in str(await write.execute(path=str(mcp), content="{}"))
    assert DENIAL in str(await write.execute(path=str(data_dir / "auth" / "new.json"), content="{}"))
    assert mcp.read_text(encoding="utf-8") == before
    assert not (data_dir / "auth" / "new.json").exists()


async def test_auth_dir_denied_even_when_allowlisted(data_dir: Path, workspace: Path) -> None:
    mcp = data_dir / "auth" / "mcp.json"
    read = ReadFileTool(
        workspace=workspace,
        allowed_dir=workspace,
        extra_read_allowed_dirs=[data_dir / "auth"],
        extra_read_allowed_files=[mcp],
    )
    result = await read.execute(path=str(mcp))
    assert DENIAL in str(result)
    assert SECRET not in str(result)

    write = WriteFileTool(
        workspace=workspace,
        allowed_dir=workspace,
        extra_write_allowed_dirs=[data_dir / "auth"],
    )
    assert DENIAL in str(await write.execute(path=str(mcp), content="{}"))


async def test_auth_dir_denied_in_flat_layout_with_restriction(data_dir: Path) -> None:
    """Flat layout: the workspace *is* the data dir, so auth/ sits inside it."""
    read = ReadFileTool(workspace=data_dir, allowed_dir=data_dir)
    result = await read.execute(path="auth/mcp.json")
    assert DENIAL in str(result)
    assert SECRET not in str(result)
    ok = await read.execute(path="notes.txt")
    assert "public" in str(ok)


async def test_plugin_data_write_and_read_denied(data_dir: Path, workspace: Path) -> None:
    marker = data_dir / "plugin-data" / "abc" / "demo" / ".enabled"
    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    assert DENIAL in str(await write.execute(path=str(marker), content="x"))
    assert not marker.exists()
    read = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    state = data_dir / "plugin-data" / "abc" / "demo" / "state.txt"
    assert DENIAL in str(await read.execute(path=str(state)))


async def test_symlink_into_auth_dir_denied(data_dir: Path, workspace: Path) -> None:
    (workspace / "creds").symlink_to(data_dir / "auth")
    read = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    result = await read.execute(path="creds/mcp.json")
    assert SECRET not in str(result)
    assert result.is_error

    unrestricted = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    result = await unrestricted.execute(path="creds/mcp.json")
    assert DENIAL in str(result)
    assert SECRET not in str(result)


async def test_sessions_root_read_and_write_denied(data_dir: Path, workspace: Path) -> None:
    root = default_sessions_root(workspace.resolve())
    (root / "abc").mkdir(parents=True)
    db = root / "abc" / "sessions.db"
    db.write_text(SECRET, encoding="utf-8")

    read = ReadFileTool(workspace=workspace, restrict_to_workspace=False)
    result = await read.execute(path=str(db))
    assert DENIAL in str(result)
    assert SECRET not in str(result)
    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    assert DENIAL in str(await write.execute(path=str(db), content="x"))
    assert db.read_text(encoding="utf-8") == SECRET


# ---------------------------------------------------------------------------
# traversal tools: grep / find_files / list_dir
# ---------------------------------------------------------------------------


async def test_find_files_and_grep_do_not_leak_protected_content(
    data_dir: Path, workspace: Path,
) -> None:
    grep = GrepTool(workspace=workspace, restrict_to_workspace=False)
    result = await grep.execute(pattern=SECRET[:10], path=str(data_dir))
    assert SECRET not in str(result)
    assert "mcp.json" not in str(result)
    assert "state.txt" not in str(result)
    # A non-protected file in the data dir is still searched.
    notes = await grep.execute(pattern="public", path=str(data_dir))
    assert "notes.txt" in str(notes)

    direct = await grep.execute(pattern="token", path=str(data_dir / "auth" / "mcp.json"))
    assert DENIAL in str(direct)
    assert SECRET not in str(direct)

    find = FindFilesTool(workspace=workspace, restrict_to_workspace=False)
    listing = await find.execute(path=str(data_dir), include_dirs=True, head_limit=0)
    assert "notes.txt" in str(listing)
    assert "mcp.json" not in str(listing)
    assert "auth" not in str(listing)
    assert "plugin-data" not in str(listing)
    assert DENIAL in str(await find.execute(path=str(data_dir / "auth")))

    lister = ListDirTool(workspace=workspace, restrict_to_workspace=False)
    flat = await lister.execute(path=str(data_dir))
    assert "notes.txt" in str(flat)
    assert "auth" not in str(flat)
    deep = await lister.execute(path=str(data_dir), recursive=True)
    assert "notes.txt" in str(deep)
    assert "mcp.json" not in str(deep)
    assert "state.txt" not in str(deep)
    assert DENIAL in str(await lister.execute(path=str(data_dir / "auth")))


@needs_proc
async def test_grep_over_workspace_symlink_to_proc_environ_does_not_leak(
    workspace: Path,
) -> None:
    (workspace / "env").symlink_to("/proc/self/environ")
    (workspace / "plain.txt").write_text("PATH=/visible\n", encoding="utf-8")
    grep = GrepTool(workspace=workspace, restrict_to_workspace=False)
    result = await grep.execute(pattern="PATH=", path=".", output_mode="files_with_matches")
    assert "plain.txt" in str(result)
    assert "env" not in str(result).replace("plain.txt", "")
    content = await grep.execute(pattern="PATH=", path=".")
    assert "/visible" in str(content)
    assert os.environ.get("PATH", "unset-path") not in str(content)


@needs_proc
async def test_find_files_and_list_dir_skip_proc_secrets(workspace: Path) -> None:
    find = FindFilesTool(workspace=workspace, restrict_to_workspace=False)
    listing = str(await find.execute(path="/proc/self", query="environ", head_limit=0))
    assert "environ" not in listing
    status = str(await find.execute(path="/proc/self", query="status", head_limit=0))
    assert "status" in status

    lister = ListDirTool(workspace=workspace, restrict_to_workspace=False)
    listing = str(await lister.execute(path="/proc/self", max_entries=1000))
    names = {line.split(" ", 1)[-1] for line in listing.splitlines()}
    assert names.isdisjoint({"environ", "mem", "maps", "root", "cwd", "exe"})
    assert {"status", "cmdline"} <= names


# ---------------------------------------------------------------------------
# image generation reference images
# ---------------------------------------------------------------------------


async def test_image_reference_in_auth_dir_denied(data_dir: Path) -> None:
    (data_dir / "auth" / "ref.png").write_bytes(PNG_BYTES)
    (data_dir / "ok.png").write_bytes(PNG_BYTES)
    tool = ImageGenerationTool(
        workspace=data_dir,
        config=ImageGenerationToolConfig(enabled=True),
        provider_config=ProviderConfig(api_key="sk-or-test"),
    )
    with pytest.raises(ImageGenerationError, match=r"protected internal path \(not configurable\)"):
        tool._resolve_reference_image("auth/ref.png")
    assert tool._resolve_reference_image("ok.png") == str((data_dir / "ok.png").resolve())


# ---------------------------------------------------------------------------
# escalation
# ---------------------------------------------------------------------------


async def test_repeated_denial_escalates_as_workspace_violation(workspace: Path) -> None:
    write = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
    arguments = {"path": "memory/history.jsonl", "content": "forged\n"}
    denial = await write.execute(**arguments)
    assert DENIAL in str(denial)
    assert AgentRunner._is_workspace_violation(str(denial))

    counts: dict[str, int] = {}
    assert repeated_workspace_violation_error("write_file", arguments, counts) is None
    assert repeated_workspace_violation_error("write_file", arguments, counts) is None
    escalated = repeated_workspace_violation_error("write_file", arguments, counts)
    assert escalated is not None
    assert "refusing repeated workspace-bypass attempts" in escalated


async def test_runner_escalates_third_protected_write(workspace: Path) -> None:
    tools = ToolRegistry()
    responses = [
        LLMResponse(content=f"try {i}", tool_calls=[ToolCallRequest(
            id=f"c{i}", name="write_file",
            arguments={"path": "memory/history.jsonl", "content": f"forged {i}\n"},
        )])
        for i in range(3)
    ]
    responses.append(LLMResponse(content="telling the user", tool_calls=[]))
    tools.register(WriteFileTool(workspace=workspace, restrict_to_workspace=False))
    provider = MagicMock(spec=LLMProvider)
    pending = list(responses)

    async def request(**_kwargs):
        return pending.pop(0)

    provider.chat_stream_with_retry = request
    result = await AgentRunner().run(make_run_spec(
        provider, model="test-model", tools=tools,
        initial_messages=[{"role": "user", "content": "overwrite the history"}],
        max_iterations=6, max_tool_result_chars=128_000,
    ))

    assert result.error is None
    assert result.final_content == "telling the user"
    details = [ev["detail"] for ev in result.tool_events if ev["status"] == "error"]
    assert len(details) == 3
    assert details[0].startswith("workspace_violation:")
    assert details[2].startswith("workspace_violation_escalated:")
    assert "forged" not in (workspace / "memory" / "history.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dream's scoped writes keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flat_layout", [False, True])
async def test_dream_scoped_writes_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flat_layout: bool,
) -> None:
    ws = tmp_path / "dream-ws"
    ws.mkdir()
    if flat_layout:
        monkeypatch.setattr("nanobot.config.loader._current_config_path", ws / "config.json")
    store = MemoryStore(ws)
    store.write_memory("# Memory\nProject X active\n")
    store.write_soul("# Soul\n")
    store.write_user("# User\n")
    tools = store.build_dream_tools()

    memory = await tools.execute("write_file", {
        "path": "memory/MEMORY.md", "content": "# Memory\nProject Y active\n",
    })
    soul = await tools.execute("edit_file", {
        "path": "SOUL.md", "old_text": "# Soul", "new_text": "# Soul v2",
    })
    user = await tools.execute("apply_patch", {"edits": [{
        "path": "USER.md", "action": "replace", "old_text": "# User", "new_text": "# User v2",
    }]})
    skill = await tools.execute("write_file", {
        "path": "skills/demo/SKILL.md",
        "content": "---\nname: demo\ndescription: Demo.\n---\n",
    })
    history = await tools.execute("read_file", {"path": "memory/history.jsonl"})

    for result in (memory, soul, user, skill, history):
        assert DENIAL not in str(result)
    assert "Project Y active" in (ws / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "# Soul v2" in (ws / "SOUL.md").read_text(encoding="utf-8")
    assert "# User v2" in (ws / "USER.md").read_text(encoding="utf-8")
    assert (ws / "skills" / "demo" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# config.json as a symlink: auth/sessions live next to the LINK (get_data_dir)
# while plugin-data lives next to the TARGET; both bases are protected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symlinked", [True, False])
async def test_symlinked_config_protects_link_and_target_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlinked: bool,
) -> None:
    link_dir = tmp_path / "home-nanobot"
    target_dir = tmp_path / "dotfiles"
    bases = [link_dir, target_dir] if symlinked else [link_dir]
    for base in (link_dir, target_dir):
        (base / "auth").mkdir(parents=True)
        (base / "auth" / "mcp.json").write_text(f'{{"token": "{SECRET}"}}\n', encoding="utf-8")
        (base / "notes.txt").write_text("public note\n", encoding="utf-8")
    (target_dir / "config.json").write_text("{}\n", encoding="utf-8")
    if symlinked:
        (link_dir / "config.json").symlink_to(target_dir / "config.json")
    else:
        (link_dir / "config.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("nanobot.config.loader._current_config_path", link_dir / "config.json")

    ws = tmp_path / "ws"
    ws.mkdir()
    read = ReadFileTool(workspace=ws, restrict_to_workspace=False)
    write = WriteFileTool(workspace=ws, restrict_to_workspace=False)

    for base in bases:
        mcp = base / "auth" / "mcp.json"
        result = await read.execute(path=str(mcp))
        assert DENIAL in str(result), mcp
        assert SECRET not in str(result)
        assert DENIAL in str(await write.execute(path=str(mcp), content="{}")), mcp
        assert SECRET in mcp.read_text(encoding="utf-8")
        sessions = base / "sessions" / "x.jsonl"
        assert DENIAL in str(await write.execute(path=str(sessions), content="x")), sessions
        assert "public note" in str(await read.execute(path=str(base / "notes.txt")))

    if not symlinked:
        # A plain config keeps exactly the old behaviour: an unrelated dir is untouched.
        other = target_dir / "auth" / "mcp.json"
        assert SECRET in str(await read.execute(path=str(other)))
