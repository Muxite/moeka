"""Integration tests for filesystem tools — real file I/O, no mocks.

ReadFileTool, WriteFileTool, EditFileTool, and ListDirTool are exercised
against actual temporary files and directories on disk.

All tools return strings directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


# ---------------------------------------------------------------------------
# ReadFileTool — real files
# ---------------------------------------------------------------------------

class TestReadFileToolReal:

    @pytest.fixture
    def tool(self) -> ReadFileTool:
        return ReadFileTool()

    @pytest.mark.asyncio
    async def test_reads_plain_text_file(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("Hello, integration!\n")
        result = await tool.execute(path=str(f))
        assert "Hello, integration!" in result

    @pytest.mark.asyncio
    async def test_reads_utf8_with_multibyte_chars(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "unicode.txt"
        f.write_text("日本語テスト\nEmoji: 🎉\n", encoding="utf-8")
        result = await tool.execute(path=str(f))
        assert "日本語テスト" in result
        assert "🎉" in result

    @pytest.mark.asyncio
    async def test_line_numbers_present(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "nums.txt"
        f.write_text("alpha\nbeta\ngamma\n")
        result = await tool.execute(path=str(f))
        assert "1|" in result
        assert "alpha" in result

    @pytest.mark.asyncio
    async def test_offset_and_limit(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "many.txt"
        lines = [f"line{i}" for i in range(1, 21)]
        f.write_text("\n".join(lines) + "\n")
        result = await tool.execute(path=str(f), offset=10, limit=5)
        # offset=10 means start at line 10 (1-indexed); first few lines must be absent
        assert "line1\n" not in result
        assert "line" in result  # some lines should be present

    @pytest.mark.asyncio
    async def test_large_file_is_readable(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "large.txt"
        f.write_text("\n".join(f"row {i}" for i in range(3000)))
        result = await tool.execute(path=str(f))
        assert "row 0" in result

    @pytest.mark.asyncio
    async def test_missing_file_returns_error(self, tool: ReadFileTool, tmp_path: Path) -> None:
        result = await tool.execute(path=str(tmp_path / "nope.txt"))
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_reads_json_file(self, tool: ReadFileTool, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        payload = {"key": "value", "numbers": [1, 2, 3]}
        f.write_text(json.dumps(payload))
        result = await tool.execute(path=str(f))
        assert '"key"' in result
        assert '"value"' in result


# ---------------------------------------------------------------------------
# WriteFileTool — real files
# ---------------------------------------------------------------------------

class TestWriteFileToolReal:

    @pytest.fixture
    def tool(self) -> WriteFileTool:
        return WriteFileTool()

    @pytest.mark.asyncio
    async def test_creates_new_file(self, tool: WriteFileTool, tmp_path: Path) -> None:
        dest = tmp_path / "new.txt"
        await tool.execute(path=str(dest), content="created by integration test")
        assert dest.exists()
        assert "created by integration test" in dest.read_text()

    @pytest.mark.asyncio
    async def test_overwrites_existing_file(self, tool: WriteFileTool, tmp_path: Path) -> None:
        dest = tmp_path / "overwrite.txt"
        dest.write_text("original")
        await tool.execute(path=str(dest), content="replaced")
        assert dest.read_text() == "replaced"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, tool: WriteFileTool, tmp_path: Path) -> None:
        dest = tmp_path / "a" / "b" / "c" / "deep.txt"
        await tool.execute(path=str(dest), content="deep content")
        assert dest.exists()
        assert dest.read_text() == "deep content"

    @pytest.mark.asyncio
    async def test_writes_unicode(self, tool: WriteFileTool, tmp_path: Path) -> None:
        dest = tmp_path / "unicode.txt"
        await tool.execute(path=str(dest), content="こんにちは 🌸")
        assert "こんにちは" in dest.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        dest = tmp_path / "roundtrip.txt"
        text = "roundtrip content — line 1\nroundtrip content — line 2\n"
        await tool.execute(path=str(dest), content=text)
        assert dest.read_text() == text

    @pytest.mark.asyncio
    async def test_writes_json_content(self, tool: WriteFileTool, tmp_path: Path) -> None:
        dest = tmp_path / "output.json"
        payload = json.dumps({"status": "ok", "value": 42})
        await tool.execute(path=str(dest), content=payload)
        loaded = json.loads(dest.read_text())
        assert loaded["status"] == "ok"
        assert loaded["value"] == 42


# ---------------------------------------------------------------------------
# EditFileTool — real files (uses old_text / new_text parameter names)
# ---------------------------------------------------------------------------

class TestEditFileToolReal:

    @pytest.fixture
    def tool(self) -> EditFileTool:
        return EditFileTool()

    @pytest.mark.asyncio
    async def test_exact_string_replacement(self, tool: EditFileTool, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        f.write_text("Hello old world\n")
        await tool.execute(path=str(f), old_text="old world", new_text="new world")
        assert f.read_text() == "Hello new world\n"

    @pytest.mark.asyncio
    async def test_replace_preserves_surrounding_content(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        f = tmp_path / "preserve.txt"
        f.write_text("line1\nTARGET\nline3\n")
        await tool.execute(path=str(f), old_text="TARGET", new_text="REPLACED")
        content = f.read_text()
        assert "line1" in content
        assert "REPLACED" in content
        assert "line3" in content
        assert "TARGET" not in content

    @pytest.mark.asyncio
    async def test_replace_all_replaces_every_occurrence(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        f = tmp_path / "all.txt"
        f.write_text("foo bar foo baz foo\n")
        await tool.execute(
            path=str(f), old_text="foo", new_text="qux", replace_all=True
        )
        assert "foo" not in f.read_text()
        assert f.read_text().count("qux") == 3

    @pytest.mark.asyncio
    async def test_edit_nonexistent_file_returns_error(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        result = await tool.execute(
            path=str(tmp_path / "missing.txt"), old_text="x", new_text="y"
        )
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_ambiguous_match_returns_error(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        f = tmp_path / "ambiguous.txt"
        f.write_text("dup\nother\ndup\n")
        result = await tool.execute(path=str(f), old_text="dup", new_text="x")
        assert any(kw in result.lower() for kw in ["ambiguous", "multiple", "appears", "times", "found", "error", "warning"])


# ---------------------------------------------------------------------------
# ListDirTool — real directories
# ---------------------------------------------------------------------------

class TestListDirToolReal:

    @pytest.fixture
    def tool(self) -> ListDirTool:
        return ListDirTool()

    @pytest.mark.asyncio
    async def test_lists_files_in_directory(self, tool: ListDirTool, tmp_path: Path) -> None:
        (tmp_path / "alpha.txt").write_text("a")
        (tmp_path / "beta.txt").write_text("b")
        result = await tool.execute(path=str(tmp_path))
        assert "alpha.txt" in result
        assert "beta.txt" in result

    @pytest.mark.asyncio
    async def test_recursive_listing(self, tool: ListDirTool, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("n")
        result = await tool.execute(path=str(tmp_path), recursive=True)
        assert "nested.txt" in result

    @pytest.mark.asyncio
    async def test_empty_directory_does_not_crash(self, tool: ListDirTool, tmp_path: Path) -> None:
        result = await tool.execute(path=str(tmp_path))
        assert result is not None

    @pytest.mark.asyncio
    async def test_missing_directory_returns_error(
        self, tool: ListDirTool, tmp_path: Path
    ) -> None:
        result = await tool.execute(path=str(tmp_path / "nope"))
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_lists_both_files_and_subdirs(self, tool: ListDirTool, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "subdir").mkdir()
        result = await tool.execute(path=str(tmp_path))
        assert "file.txt" in result
        assert "subdir" in result
