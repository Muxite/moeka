"""Built-in tool descriptions as data files (Task 20).

- ``nanobot/agent/tools/descriptions/<tool>.txt`` holds a built-in tool's description;
  the tool reads it through the ``description_from_file`` descriptor in the tool base.
- The text is byte-identical to the former Python literal (the golden snapshot in
  ``test_description_golden.py`` pins what the model sees).
- ``exec`` and ``my`` keep Python descriptions: theirs are built at runtime (platform
  branch / ``allow_set`` config), see the Task 20 ruling.
- Description file hashes: ``compute_descriptions_hash`` (kernel manifest module) is the
  same per-file fold ``compute_version_hash`` applies to a plugin's ``descriptions``.
"""

from __future__ import annotations

import hashlib
import inspect
import shutil
from pathlib import Path

import pytest

from nanobot.agent.tools import base
from nanobot.agent.tools.base import (
    DESCRIPTIONS_DIR,
    DescriptionFileError,
    Tool,
    builtin_description,
    builtin_description_files,
    builtin_description_hashes,
    builtin_descriptions_hash,
    description_from_file,
    description_source,
    read_description_file,
)
from nanobot.kernel.manifest import compute_descriptions_hash, compute_version_hash

# Built-in tools whose description now lives in a data file.
MIGRATED = {
    "apply_patch", "ask_user", "bg_shell", "create_goal", "defer_action", "edit_file",
    "exec_session", "find_files", "generate_image", "grep", "list_dir",
    "list_exec_sessions", "list_sessions", "read_file", "read_session",
    "search_sessions", "send_session_message", "spawn", "update_goal", "web_fetch",
    "web_search", "write_file",
}
# Runtime-built descriptions that stay Python (ruling recorded in the Task 20 report).
DYNAMIC = {"exec", "my"}


def _builtin_classes() -> dict[str, type[Tool]]:
    from nanobot.agent.tools.apply_patch import ApplyPatchTool
    from nanobot.agent.tools.ask import AskUserTool
    from nanobot.agent.tools.bg_shell import BackgroundShellTool
    from nanobot.agent.tools.defer import DeferActionTool
    from nanobot.agent.tools.exec_session import ExecSessionTool, ListExecSessionsTool
    from nanobot.agent.tools.filesystem import (
        EditFileTool,
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from nanobot.agent.tools.image_generation import ImageGenerationTool
    from nanobot.agent.tools.long_task import CreateGoalTool, UpdateGoalTool
    from nanobot.agent.tools.search import FindFilesTool, GrepTool
    from nanobot.agent.tools.self import MyTool
    from nanobot.agent.tools.session_messages import ListSessionsTool, SendSessionMessageTool
    from nanobot.agent.tools.sessions import ReadSessionTool, SearchSessionsTool
    from nanobot.agent.tools.shell import ExecTool
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.agent.tools.web import WebFetchTool, WebSearchTool

    return {
        "apply_patch": ApplyPatchTool, "ask_user": AskUserTool,
        "bg_shell": BackgroundShellTool, "create_goal": CreateGoalTool,
        "defer_action": DeferActionTool, "edit_file": EditFileTool,
        "exec_session": ExecSessionTool, "find_files": FindFilesTool,
        "generate_image": ImageGenerationTool, "grep": GrepTool, "list_dir": ListDirTool,
        "list_exec_sessions": ListExecSessionsTool, "list_sessions": ListSessionsTool,
        "read_file": ReadFileTool, "read_session": ReadSessionTool,
        "search_sessions": SearchSessionsTool, "send_session_message": SendSessionMessageTool,
        "spawn": SpawnTool, "update_goal": UpdateGoalTool, "web_fetch": WebFetchTool,
        "web_search": WebSearchTool, "write_file": WriteFileTool,
        "exec": ExecTool, "my": MyTool,
    }


# -- the data files ---------------------------------------------------------------


def test_every_migrated_tool_has_exactly_one_data_file() -> None:
    assert set(builtin_description_files()) == MIGRATED
    assert {p.stem for p in DESCRIPTIONS_DIR.iterdir()} == MIGRATED
    assert all(p.suffix == ".txt" for p in DESCRIPTIONS_DIR.iterdir())


@pytest.mark.parametrize("name", sorted(MIGRATED))
def test_migrated_class_reads_its_own_data_file(name: str) -> None:
    cls = _builtin_classes()[name]
    assert description_source(cls) == DESCRIPTIONS_DIR / f"{name}.txt"
    # Class access returns the descriptor (as ``property`` does); ``path`` is its file.
    assert cls.description.path == DESCRIPTIONS_DIR / f"{name}.txt"


@pytest.mark.parametrize("name", sorted(DYNAMIC))
def test_dynamic_descriptions_stay_python(name: str) -> None:
    assert description_source(_builtin_classes()[name]) is None
    assert not (DESCRIPTIONS_DIR / f"{name}.txt").exists()


@pytest.mark.parametrize("name", sorted(MIGRATED))
def test_data_file_is_one_text_plus_one_newline(name: str) -> None:
    raw = (DESCRIPTIONS_DIR / f"{name}.txt").read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert raw.decode("utf-8")[:-1] == builtin_description(name)


def test_wheel_build_includes_description_files() -> None:
    pyproject = (Path(base.__file__).parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"nanobot/agent/tools/descriptions/*.txt"' in pyproject


def test_descriptions_dir_is_not_a_tool_module() -> None:
    """No ``__init__.py``: the loader's pkgutil scan never treats it as a tool module."""
    assert not (DESCRIPTIONS_DIR / "__init__.py").exists()


# -- reading a description file ----------------------------------------------------


def test_read_strips_exactly_one_trailing_newline(tmp_path) -> None:
    f = tmp_path / "t.txt"
    f.write_bytes(b"line one\nline two\n\n")
    assert read_description_file(f) == "line one\nline two\n"
    f.write_bytes(b"no newline")
    assert read_description_file(f) == "no newline"


def test_read_missing_file_is_a_clear_error(tmp_path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(DescriptionFileError) as err:
        read_description_file(missing, tool="nope")
    assert "'nope'" in str(err.value) and str(missing) in str(err.value)


def test_read_non_utf8_is_a_clear_error(tmp_path) -> None:
    f = tmp_path / "bad.txt"
    f.write_bytes(b"\xff\xfe bad")
    with pytest.raises(DescriptionFileError, match="UTF-8"):
        read_description_file(f, tool="bad")


def test_descriptor_defers_a_missing_file_to_access_time() -> None:
    """Defining the class (importing the module) never fails; access raises clearly."""

    class Ghost(Tool):
        name = "ghost_tool"  # pyright: ignore[reportAssignmentType]
        description = description_from_file("ghost_tool_missing")  # pyright: ignore
        parameters = {"type": "object", "properties": {}}  # pyright: ignore

        async def execute(self, **kwargs):
            return ""

    tool = Ghost()
    with pytest.raises(DescriptionFileError, match="ghost_tool_missing"):
        _ = tool.description


def test_descriptor_rejects_a_path_like_name() -> None:
    for bad in ("../etc/passwd", "a/b", "", "Upper"):
        with pytest.raises(ValueError):
            description_from_file(bad)


def test_descriptor_is_found_statically() -> None:
    from nanobot.agent.tools.spawn import SpawnTool

    assert isinstance(inspect.getattr_static(SpawnTool, "description"), description_from_file)


# -- hashes ------------------------------------------------------------------------


def test_builtin_description_hashes_are_per_file_sha256() -> None:
    hashes = builtin_description_hashes()
    assert set(hashes) == MIGRATED
    for name, digest in hashes.items():
        expected = compute_descriptions_hash(DESCRIPTIONS_DIR, {name: f"{name}.txt"})
        assert digest == expected and len(digest) == 64


def test_builtin_descriptions_hash_is_the_manifest_fold() -> None:
    assert builtin_descriptions_hash() == compute_descriptions_hash(
        DESCRIPTIONS_DIR, builtin_description_files()
    )


def test_builtin_descriptions_hash_changes_with_one_byte(tmp_path) -> None:
    copy = tmp_path / "descriptions"
    shutil.copytree(DESCRIPTIONS_DIR, copy)
    before = builtin_descriptions_hash(copy)
    assert before == builtin_descriptions_hash()
    target = copy / "spawn.txt"
    target.write_bytes(target.read_bytes().replace(b"Spawn", b"spawn", 1))
    assert builtin_descriptions_hash(copy) != before


def test_descriptions_hash_covers_name_path_and_bytes(tmp_path) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha\n")
    (tmp_path / "b.txt").write_bytes(b"alpha\n")
    base_hash = compute_descriptions_hash(tmp_path, {"a": "a.txt"})
    assert compute_descriptions_hash(tmp_path, {"a": "a.txt"}) == base_hash
    assert compute_descriptions_hash(tmp_path, {"a": "b.txt"}) != base_hash
    assert compute_descriptions_hash(tmp_path, {"z": "a.txt"}) != base_hash
    # Domain-separated from a bare sha256 of the file.
    assert base_hash != hashlib.sha256(b"alpha\n").hexdigest()


def test_descriptions_hash_rejects_escape_and_missing(tmp_path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        compute_descriptions_hash(root, {"t": "../outside.txt"})
    with pytest.raises(ValueError, match="unreadable"):
        compute_descriptions_hash(root, {"t": "missing.txt"})


def test_version_hash_unchanged_by_the_shared_fold(tmp_path) -> None:
    """The fold was factored out of ``compute_version_hash``; its value is pinned."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "tool.py").write_text("pass\n", encoding="utf-8")
    (pkg / "desc").mkdir()
    (pkg / "desc" / "t.txt").write_bytes(b"Do a thing.\n")
    raw = {
        "name": "p", "kind": "tool", "version": "1.0.0", "version_hash": "0" * 64,
        "tier": 2, "entry": "pkg.tool:T", "descriptions": {"t": "desc/t.txt"},
    }
    # Computed with the Task 17 implementation before the refactor.
    assert compute_version_hash(pkg, raw) == (
        "2a6d90bd72b31689e1e3f74912281a0c3c2f5bad0115493f6ebf3db123cf5665"
    )
