"""Tests for grep search tools."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.search import FindFilesTool, GrepTool
from nanobot.agent.tools.web import WebSearchConfig, WebSearchTool
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    default_workspace_scope,
    reset_workspace_scope,
)
from nanobot.utils.llm_runtime import LLMRuntime


@pytest.mark.asyncio
async def test_web_search_tool_refreshes_dynamic_config_loader(monkeypatch) -> None:
    tool = WebSearchTool(
        config=WebSearchConfig(provider="brave"),
        config_loader=lambda: WebSearchConfig(provider="duckduckgo", max_results=3),
    )

    async def fake_duckduckgo(self, query: str, n: int) -> str:
        return f"{self.config.provider}:{query}:{n}"

    monkeypatch.setattr(WebSearchTool, "_search_duckduckgo", fake_duckduckgo)

    assert await tool.execute("nanobot") == "duckduckgo:nanobot:3"


@pytest.mark.asyncio
async def test_find_files_filters_by_query_glob_and_type(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "settings_view.tsx").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "src" / "settings_api.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("settings\n", encoding="utf-8")

    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        path=".",
        query="settings",
        glob="src/**",
        type="ts",
    )

    assert result.splitlines() == ["src/settings_view.tsx"]


@pytest.mark.parametrize("tool_name", ["find_files", "grep"])
@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("**/*.py", ["main.py", "src/api.py", "src/nested/deep/worker.py"]),
        ("src/**/*.py", ["src/api.py", "src/nested/deep/worker.py"]),
        ("src/**", ["src/api.py", "src/nested/deep/worker.py"]),
        ("src/*.py", ["src/api.py"]),
        ("src/**/deep/*.py", ["src/nested/deep/worker.py"]),
        (r"src\**\*.py", ["src/api.py", "src/nested/deep/worker.py"]),
    ],
)
async def test_search_recursive_glob_matches_zero_or_more_directories(
    tmp_path: Path, tool_name: str, glob: str, expected: list[str]
) -> None:
    for name in ["main.py", "src/api.py", "src/nested/deep/worker.py", "notes.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n", encoding="utf-8")

    if tool_name == "find_files":
        result = await FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path).execute(
            path=".", glob=glob,
        )
    else:
        result = await GrepTool(workspace=tmp_path, allowed_dir=tmp_path).execute(
            pattern="needle", path=".", glob=glob, output_mode="files_with_matches",
        )

    assert sorted(result.splitlines()) == expected


@pytest.mark.asyncio
async def test_find_files_can_include_directories(tmp_path: Path) -> None:
    (tmp_path / "src" / "settings").mkdir(parents=True)
    (tmp_path / "src" / "settings" / "index.ts").write_text("export {}\n", encoding="utf-8")

    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(path="src", query="settings", include_dirs=True)

    assert "src/settings/" in result.splitlines()
    assert "src/settings/index.ts" in result.splitlines()


@pytest.mark.asyncio
async def test_find_files_supports_modified_sort_and_pagination(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for idx, name in enumerate(("a.py", "b.py", "c.py"), start=1):
        file_path = tmp_path / "src" / name
        file_path.write_text("pass\n", encoding="utf-8")
        os.utime(file_path, (idx, idx))

    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        path="src",
        type="py",
        sort="modified",
        head_limit=1,
        offset=1,
    )

    assert result.splitlines()[0] == "src/b.py"
    assert "pagination: limit=1, offset=1" in result


@pytest.mark.asyncio
async def test_find_files_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-find-files.txt"
    outside.write_text("secret\n", encoding="utf-8")

    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(path=str(outside))

    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_find_files_worker_preserves_current_workspace_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "inside.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("nope\n", encoding="utf-8")
    tool = FindFilesTool(workspace=tmp_path, restrict_to_workspace=False)
    token = bind_workspace_scope(default_workspace_scope(project, True))
    try:
        result = await tool.execute(path=".")
    finally:
        reset_workspace_scope(token)

    assert result == "inside.txt"


@pytest.mark.asyncio
async def test_find_files_scan_keeps_event_loop_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "match.txt"
    target.write_text("ok\n", encoding="utf-8")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    original_iter_paths = tool._iter_paths
    started = threading.Event()
    release = threading.Event()

    def blocking_iter_paths(root: Path, *, include_dirs: bool, budget):
        started.set()
        if not release.wait(timeout=1):
            raise TimeoutError("test did not release find_files traversal")
        yield from original_iter_paths(root, include_dirs=include_dirs, budget=budget)

    monkeypatch.setattr(tool, "_iter_paths", blocking_iter_paths)
    task = asyncio.create_task(tool.execute(path="."))
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        for _ in range(3):
            await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()

    assert await asyncio.wait_for(task, timeout=0.5) == "match.txt"


@pytest.mark.asyncio
async def test_find_files_cancellation_stops_worker_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    started = threading.Event()
    stopped = threading.Event()

    def cancellable_iter_paths(root: Path, *, include_dirs: bool, budget):
        del root, include_dirs
        started.set()
        if not budget.cancelled.wait(timeout=1):
            raise TimeoutError("find_files worker did not receive cancellation")
        stopped.set()
        if False:
            yield

    monkeypatch.setattr(tool, "_iter_paths", cancellable_iter_paths)
    task = asyncio.create_task(tool.execute(path="."))
    assert await asyncio.to_thread(started.wait, 0.5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert await asyncio.to_thread(stopped.wait, 0.5)


@pytest.mark.asyncio
async def test_find_files_path_limit_stops_after_pagination_lookahead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / name for name in ("a.txt", "b.txt", "c.txt")]
    for path in paths:
        path.write_text("ok\n", encoding="utf-8")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    visited: list[str] = []

    def ordered_iter_paths(root: Path, *, include_dirs: bool, budget):
        del include_dirs
        for path in paths:
            budget.checkpoint()
            visited.append(path.name)
            yield tool._entry(path, root, is_dir=False)

    monkeypatch.setattr(tool, "_iter_paths", ordered_iter_paths)
    result = await tool.execute(path=".", head_limit=1)

    assert result.splitlines()[0] == "a.txt"
    assert "pagination: limit=1, offset=0" in result
    assert visited == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_find_files_path_budget_counts_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    monkeypatch.setattr(FindFilesTool, "_MAX_SCAN_PATHS", 1)
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(path=".")

    assert result.startswith("Error: find_files scan exceeded 1 paths")


@pytest.mark.asyncio
async def test_find_files_enforces_time_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "match.txt").write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(FindFilesTool, "_MAX_SCAN_SECONDS", 0.0)
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(path=".")

    assert result.startswith("Error: find_files scan exceeded 0 seconds")


@pytest.mark.asyncio
async def test_find_files_path_sort_matches_existing_lexicographic_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "inside.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "a+").write_text("ok\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("ok\n", encoding="utf-8")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(path=".", head_limit=0)

    assert result.splitlines() == ["a+", "a.py", "a/inside.txt"]


@pytest.mark.asyncio
async def test_grep_respects_glob_filter_and_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "alpha\nbeta\nmatch_here\ngamma\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="match_here",
        path=".",
        glob="*.py",
        output_mode="content",
        context_before=1,
        context_after=1,
    )

    assert "src/main.py:3" in result
    assert "  2| beta" in result
    assert "> 3| match_here" in result
    assert "  4| gamma" in result
    assert "README.md" not in result


@pytest.mark.asyncio
async def test_grep_defaults_to_match_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 6))
        + "\nmatch_here\n"
        + "\n".join(f"line {line}" for line in range(7, 13)),
        encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="match_here",
        path="src",
    )

    assert "src/main.py:6" in result
    assert "  1| line 1" in result
    assert "> 6| match_here" in result
    assert "  11| line 11" in result
    assert "line 12" not in result


@pytest.mark.asyncio
async def test_grep_searches_xlsx_with_sheet_cell_locator(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook_path = tmp_path / "people.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "People"
    sheet.append(["Name", "Role"])
    sheet.append(["Ada", "Engineer"])
    workbook.save(workbook_path)
    workbook.close()

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="Engineer",
        path="people.xlsx",
        fixed_strings=True,
    )

    assert "people.xlsx:3" in result
    assert "sheet='People',row=2,cell=B2" in result
    assert "Ada\tEngineer" in result


@pytest.mark.asyncio
async def test_grep_searches_docx_with_paragraph_locator(tmp_path: Path) -> None:
    from docx import Document

    document_path = tmp_path / "notes.docx"
    document = Document()
    document.add_paragraph("Introduction")
    document.add_paragraph("late needle")
    document.save(document_path)

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="late needle",
        path="notes.docx",
        fixed_strings=True,
    )

    assert "notes.docx:3 [paragraph=2]" in result
    assert "late needle" in result


@pytest.mark.asyncio
async def test_grep_searches_pptx_with_slide_locator(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    presentation_path = tmp_path / "deck.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    textbox = slide.shapes.add_textbox(
        Inches(1), Inches(1), Inches(4), Inches(1)
    )
    textbox.text_frame.text = "slide needle"
    presentation.save(presentation_path)

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="slide needle",
        path="deck.pptx",
        fixed_strings=True,
    )

    assert "deck.pptx:2 [slide=1,line=1]" in result
    assert "slide needle" in result


@pytest.mark.asyncio
async def test_grep_searches_pdf_with_page_locator(tmp_path: Path) -> None:
    import fitz

    pdf_path = tmp_path / "notes.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "pdf needle")
    document.save(pdf_path)
    document.close()

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="pdf needle",
        path="notes.pdf",
        fixed_strings=True,
    )

    assert "notes.pdf:2 [page=1,line=1]" in result
    assert "pdf needle" in result


@pytest.mark.asyncio
async def test_grep_searches_xlsx_beyond_attachment_preview_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpyxl import Workbook

    from nanobot.utils import document as document_utils

    workbook_path = tmp_path / "long.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    for row in range(1, 20):
        sheet.append([f"ordinary-row-{row}"])
    sheet.append(["late-needle"])
    workbook.save(workbook_path)
    workbook.close()
    monkeypatch.setattr(document_utils, "_MAX_TEXT_LENGTH", 50)

    preview = document_utils.extract_text(workbook_path)
    assert preview is not None
    assert "late-needle" not in preview

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="late-needle",
        path="long.xlsx",
        fixed_strings=True,
    )

    assert "late-needle" in result
    assert "sheet='Data',row=20,cell=A20" in result
    assert "No matches found" not in result


@pytest.mark.asyncio
async def test_grep_keeps_an_oversized_matching_line_visible(tmp_path: Path) -> None:
    long_line = "x" * 130_000 + "needle" + "y" * 10_000
    (tmp_path / "huge-line.txt").write_text(long_line, encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        pattern="needle",
        path="huge-line.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
    )

    assert "huge-line.txt:1" in result
    assert "needle" in result
    assert "No matches found" not in result
    assert len(result) < GrepTool._MAX_RESULT_CHARS


@pytest.mark.asyncio
async def test_grep_size_limit_returns_a_resumable_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "\n".join(f"needle-{line}-" + "x" * 80 for line in range(1, 11))
    (tmp_path / "many.txt").write_text(content, encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_MAX_RESULT_CHARS", 350)
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    first = await tool.execute(
        pattern="needle",
        path="many.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
        head_limit=10,
    )
    continuation = re.search(r"use offset=(\d+) to continue", first)

    assert continuation is not None
    next_offset = int(continuation.group(1))
    assert next_offset > 0

    second = await tool.execute(
        pattern="needle",
        path="many.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
        head_limit=10,
        offset=next_offset,
    )
    first_headers = {
        line for line in first.splitlines() if line.startswith("many.txt:")
    }
    second_headers = {
        line for line in second.splitlines() if line.startswith("many.txt:")
    }
    assert second_headers
    assert first_headers.isdisjoint(second_headers)


@pytest.mark.asyncio
async def test_grep_reports_an_invalid_pdf_page_range(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    pdf_path = tmp_path / "one-page.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as output:
        writer.write(output)

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path="one-page.pdf", pages="bad")

    assert result.startswith("Error: Invalid PDF page range 'bad'")
    assert "binary/unreadable" not in result

    out_of_bounds = await tool.execute(
        pattern="needle",
        path="one-page.pdf",
        pages="99",
    )
    assert out_of_bounds == (
        "Error: Invalid PDF page range '99': document has 1 page; "
        "use a page number or range within 1-1."
    )


@pytest.mark.asyncio
async def test_grep_supports_case_insensitive_search(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n",
        encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="oauth",
        path="memory/HISTORY.md",
        case_insensitive=True,
        output_mode="content",
    )

    assert "memory/HISTORY.md:1" in result
    assert "OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_type_filter_limits_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        type="py",
        output_mode="files_with_matches",
    )

    assert result.splitlines() == ["src/a.py"]


@pytest.mark.asyncio
async def test_grep_fixed_strings_treats_regex_chars_literally(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n",
        encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="[2026-04-02 10:00]",
        path="memory/HISTORY.md",
        fixed_strings=True,
        output_mode="content",
    )

    assert "memory/HISTORY.md:1" in result
    assert "[2026-04-02 10:00] OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_returns_unique_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    a = tmp_path / "src" / "a.py"
    b = tmp_path / "src" / "b.py"
    a.write_text("needle\nneedle\n", encoding="utf-8")
    b.write_text("needle\n", encoding="utf-8")
    os.utime(a, (1, 1))
    os.utime(b, (2, 2))

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
    )

    assert result.splitlines() == ["src/b.py", "src/a.py"]


@pytest.mark.asyncio
async def test_grep_files_with_matches_supports_head_limit_and_offset(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
        head_limit=1,
        offset=1,
    )

    # Filesystem order is not deterministic across platforms, so just verify:
    # 1. Only one file path is returned (head_limit=1 after offset=1)
    # 2. The pagination info is correct
    assert "pagination: limit=1, offset=1" in result
    # Count non-empty lines that start with src/ (file paths)
    file_lines = [line for line in result.splitlines() if line.startswith("src/")]
    assert len(file_lines) == 1


@pytest.mark.asyncio
async def test_grep_count_mode_reports_counts_per_file(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "one.log").write_text("warn\nok\nwarn\n", encoding="utf-8")
    (tmp_path / "logs" / "two.log").write_text("warn\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="warn",
        path="logs",
        output_mode="count",
    )

    assert "logs/one.log: 2" in result
    assert "logs/two.log: 1" in result
    assert "total matches: 3 in 2 files" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_respects_max_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    files = []
    for idx, name in enumerate(("a.py", "b.py", "c.py"), start=1):
        file_path = tmp_path / "src" / name
        file_path.write_text("needle\n", encoding="utf-8")
        os.utime(file_path, (idx, idx))
        files.append(file_path)

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
        max_results=2,
    )

    assert result.splitlines()[:2] == ["src/c.py", "src/b.py"]
    assert "pagination: limit=2, offset=0" in result


@pytest.mark.asyncio
async def test_grep_reports_skipped_binary_and_large_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "large.txt").write_text("x" * 20, encoding="utf-8")

    monkeypatch.setattr(GrepTool, "_MAX_FILE_BYTES", 10)
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path=".")

    assert "No matches found" in result
    assert "skipped 1 binary/unreadable files" in result
    assert "skipped 1 large files" in result


@pytest.mark.asyncio
async def test_grep_uses_a_larger_bounded_limit_for_an_explicit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_file = tmp_path / "history.jsonl"
    large_file.write_text("needle\n" + "x" * 20, encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(GrepTool, "_MAX_EXPLICIT_FILE_BYTES", 100)
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    explicit_result = await tool.execute(
        pattern="needle",
        path=str(large_file),
        output_mode="content",
    )
    directory_result = await tool.execute(pattern="needle", path=".")
    monkeypatch.setattr(GrepTool, "_MAX_EXPLICIT_FILE_BYTES", 10)
    capped_result = await tool.execute(pattern="needle", path=str(large_file))

    assert "needle" in explicit_result
    assert "skipped 1 large files" in directory_result
    assert "skipped 1 large files" in capped_result


def test_grep_schema_is_concise_and_hides_internal_limits(tmp_path: Path) -> None:
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    properties = tool.parameters["properties"]

    assert len(tool.description) < 150
    assert "head_limit" in properties
    assert "max_matches" not in properties
    assert "max_results" not in properties


@pytest.mark.asyncio
async def test_search_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("secret\n", encoding="utf-8")

    grep_tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    grep_result = await grep_tool.execute(pattern="secret", path=str(outside))

    assert grep_result.startswith("Error:")


def test_agent_loop_registers_grep(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    assert "find_files" in loop.tools.tool_names
    assert "grep" in loop.tools.tool_names


@pytest.mark.asyncio
async def test_subagent_registers_grep(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    mgr = SubagentManager(
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=4096,
    )
    captured: dict[str, list[str]] = {}

    async def fake_run(spec):
        captured["tool_names"] = spec.tools.tool_names
        return SimpleNamespace(
            stop_reason="ok",
            final_content="done",
            tool_events=[],
            error=None,
        )

    mgr.runner.run = fake_run
    mgr._announce_result = AsyncMock()

    status = SubagentStatus(task_id="sub-1", label="label", task_description="search task", started_at=time.monotonic())
    await mgr._run_subagent(
        "sub-1",
        "search task",
        "label",
        {"channel": "cli", "chat_id": "direct"},
        status,
        LLMRuntime.capture(provider, "test-model", context_window_tokens=128_000),
    )

    assert "find_files" in captured["tool_names"]
    assert "grep" in captured["tool_names"]


def test_subagent_prompt_respects_disabled_skills(tmp_path: Path) -> None:
    bus = MessageBus()
    skills_dir = tmp_path / "skills"
    (skills_dir / "alpha").mkdir(parents=True)
    (skills_dir / "alpha" / "SKILL.md").write_text("# Alpha\n\nhidden\n", encoding="utf-8")
    (skills_dir / "beta").mkdir(parents=True)
    (skills_dir / "beta" / "SKILL.md").write_text("# Beta\n\nshown\n", encoding="utf-8")

    mgr = SubagentManager(
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=4096,
        disabled_skills=["alpha"],
    )

    prompt = mgr._build_subagent_prompt()

    assert "alpha" not in prompt
    assert "beta" in prompt


@pytest.mark.asyncio
async def test_grep_catastrophic_regex_times_out_and_loop_stays_responsive(
    tmp_path: Path,
) -> None:
    (tmp_path / "evil.txt").write_text("a" * 40 + "b\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=0.5)

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    result = await asyncio.wait_for(tool.execute(pattern=r"(a+)+$"), timeout=30)
    elapsed = time.monotonic() - started
    ticks_at_end = ticks
    stop = True
    await task

    assert result.startswith("Error: grep timed out after 0.5s (pattern too expensive)")
    assert "use a simpler pattern or narrow the search path." in result
    assert elapsed < 15
    assert ticks_at_end > 10  # the loop kept running while the regex was stuck


@pytest.mark.asyncio
async def test_grep_default_timeout_message_says_10s(tmp_path: Path) -> None:
    (tmp_path / "evil.txt").write_text("a" * 40 + "b\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=0.3)
    result = await tool.execute(pattern=r"(a+)+$")
    assert "timed out after 0.3s" in result
    assert GrepTool(workspace=tmp_path)._regex_timeout_s == 10.0


@pytest.mark.asyncio
async def test_grep_cooperative_deadline_stops_scan_between_lines(tmp_path: Path) -> None:
    for index in range(30):
        (tmp_path / f"many{index}.txt").write_text("nothing here\n" * 5_000, encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=0.001)
    result = await tool.execute(pattern=r"zzz-not-present", context_before=0, context_after=0)
    assert result.startswith("Error: grep timed out after 0.001s")


@pytest.mark.asyncio
async def test_grep_risky_looking_patterns_still_return_correct_results(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("foo bar\nbaz\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern=r"(foo|baz)+", output_mode="files_with_matches")
    assert result.strip() == "a.txt"
    result = await tool.execute(pattern=r"(\d+,)*x", output_mode="files_with_matches")
    assert "No matches" in result


@pytest.mark.asyncio
async def test_grep_regex_matches_past_50k_chars_and_anchors_see_the_real_line_end(
    tmp_path: Path,
) -> None:
    (tmp_path / "min.txt").write_text("a" * 60_000 + "NEEDLE\n", encoding="utf-8")
    (tmp_path / "anchor.txt").write_text(
        "a" * 49_997 + "foo" + "b" * 20_000 + "\n", encoding="utf-8"
    )
    tool = GrepTool(workspace=tmp_path)
    found = await tool.execute(
        pattern="NEED[L]E", path="min.txt", output_mode="files_with_matches"
    )
    assert found.strip() == "min.txt"
    # foo ends exactly at char 50 000 of a 70k line: `$` must not see a fake line end.
    anchored = await tool.execute(
        pattern="foo$", path="anchor.txt", output_mode="files_with_matches"
    )
    assert "No matches" in anchored
    lookahead = await tool.execute(
        pattern=r"foo(?!b)", path="anchor.txt", output_mode="files_with_matches"
    )
    assert "No matches" in lookahead


# ---------------------------------------------------------------------------
# Regex-only worker isolation (expensive patterns and long lines)
# ---------------------------------------------------------------------------

_SECRET_TEXT = "secret-token-value-0123456789"


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict, subprocess.Popen]]:
    """Record every worker process the grep tool spawns."""
    calls: list[tuple[tuple, dict, subprocess.Popen]] = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        calls.append((args, kwargs, proc))
        return proc

    monkeypatch.setattr("nanobot.agent.tools.search.subprocess.Popen", spy)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [r".*secret.*value", r"(s+)+ecret-token"])
async def test_grep_worker_path_keeps_the_protected_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    from nanobot.security.protected_paths import ProtectedFloor  # noqa: F401

    inst = tmp_path / "inst"
    (inst / "auth").mkdir(parents=True)
    (inst / "auth" / "token.txt").write_text(_SECRET_TEXT, encoding="utf-8")
    (inst / "plugin-data" / "x").mkdir(parents=True)
    (inst / "plugin-data" / "x" / "state.txt").write_text(_SECRET_TEXT, encoding="utf-8")
    (inst / "notes.txt").write_text("public " + _SECRET_TEXT + "\n", encoding="utf-8")
    monkeypatch.setattr("nanobot.config.loader._current_config_path", inst / "config.json")
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = GrepTool(workspace=ws, restrict_to_workspace=False)

    result = await tool.execute(pattern=pattern, path=str(inst))

    assert "notes.txt" in result
    assert "token.txt" not in result
    assert "state.txt" not in result
    assert result.count(_SECRET_TEXT) == 1  # only the public notes.txt line


@pytest.mark.asyncio
async def test_grep_long_line_worker_path_keeps_the_protected_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = tmp_path / "inst"
    (inst / "auth").mkdir(parents=True)
    (inst / "auth" / "token.txt").write_text(_SECRET_TEXT, encoding="utf-8")
    (inst / "long.txt").write_text("x" * 20_000 + "\n", encoding="utf-8")
    monkeypatch.setattr("nanobot.config.loader._current_config_path", inst / "config.json")
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = GrepTool(workspace=ws, restrict_to_workspace=False)

    result = await tool.execute(pattern=r"secret|xxxx", path=str(inst))

    assert "long.txt" in result
    assert "token.txt" not in result
    assert _SECRET_TEXT not in result


@pytest.mark.asyncio
async def test_grep_worker_ignores_cwd_shadow_modules_and_gets_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned
) -> None:
    ws = tmp_path / "ws"
    (ws / "nanobot").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    payload = f"open({str(marker)!r}, 'w').write('x')\n"
    for name in ("nanobot/__init__.py", "re.py", "json.py", "sitecustomize.py"):
        (ws / name).write_text(payload, encoding="utf-8")
    (ws / "data.txt").write_text("error one timeout\n", encoding="utf-8")
    monkeypatch.chdir(ws)
    monkeypatch.setenv("FAKE_API_KEY", "sk-should-not-leak")
    tool = GrepTool(workspace=ws, regex_timeout_s=10)

    result = await tool.execute(pattern=r".*error.*timeout", path="data.txt")

    assert "error one timeout" in result
    assert not marker.exists()
    assert len(spawned) == 1
    args, kwargs, _proc = spawned[0]
    assert "-I" in args[0]
    assert "-S" in args[0]
    assert "FAKE_API_KEY" not in kwargs["env"]
    assert not any("sk-should-not-leak" in v for v in kwargs["env"].values())
    assert Path(kwargs["cwd"]) != ws


@pytest.mark.asyncio
async def test_grep_ordinary_patterns_stay_in_process(tmp_path: Path, spawned) -> None:
    (tmp_path / "a.txt").write_text("hello world\nfoo bar\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    assert "hello world" in await tool.execute(pattern=r"hel+o")
    assert "foo bar" in await tool.execute(pattern="foo", fixed_strings=True)
    assert spawned == []


@pytest.mark.asyncio
async def test_grep_long_line_routes_any_regex_to_the_worker(tmp_path: Path, spawned) -> None:
    (tmp_path / "a.txt").write_text("x" * 12_000 + "NEEDLE\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern="NEED[L]E", output_mode="files_with_matches")
    assert result.strip() == "a.txt"
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_grep_polynomial_pattern_times_out_and_loop_stays_responsive(
    tmp_path: Path, spawned
) -> None:
    (tmp_path / "poly.txt").write_text("a" * 1500 + "\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=1)

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    result = await asyncio.wait_for(tool.execute(pattern=r"a*a*a*b"), timeout=30)
    elapsed = time.monotonic() - started
    stop = True
    await task

    assert result.startswith("Error: grep timed out after 1s")
    assert elapsed < 4
    assert ticks > 10
    assert all(proc.poll() is not None for _a, _k, proc in spawned)


@pytest.mark.asyncio
async def test_grep_quadratic_pattern_on_long_line_does_not_block_the_loop(
    tmp_path: Path,
) -> None:
    (tmp_path / "long.txt").write_text("e" * 40_000 + "\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=5)
    ticks = 0
    max_gap = 0.0
    stop = False

    async def ticker() -> None:
        nonlocal ticks, max_gap
        last = time.monotonic()
        while not stop:
            ticks += 1
            await asyncio.sleep(0)
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now

    task = asyncio.create_task(ticker())
    result = await asyncio.wait_for(tool.execute(pattern=r".*error.*timeout"), timeout=30)
    stop = True
    await task
    assert "No matches found" in result or "timed out" in result
    assert ticks > 10
    assert max_gap < 0.3


@pytest.mark.asyncio
async def test_grep_fails_closed_when_worker_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("error timeout\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr("nanobot.agent.tools.search.subprocess.Popen", boom)
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        result = await GrepTool(workspace=tmp_path).execute(pattern=r".*error.*timeout")
    finally:
        logger.remove(sink_id)

    assert result.startswith("Error: grep could not isolate an expensive pattern")
    assert "error timeout" not in result
    assert any("regex worker unavailable" in m for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("script_body", ["", "print('garbage')\n", "import sys; sys.exit(3)\n"])
async def test_grep_worker_garbage_or_dead_worker_is_a_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_body: str
) -> None:
    fake = tmp_path / "fake_worker.py"
    fake.write_text(script_body, encoding="utf-8")
    monkeypatch.setattr("nanobot.agent.tools.search._GREP_WORKER_SCRIPT", fake)
    (tmp_path / "a.txt").write_text("error timeout\n", encoding="utf-8")

    result = await GrepTool(workspace=tmp_path).execute(pattern=r".*error.*timeout")

    assert result.startswith("Error: grep could not isolate an expensive pattern")


@pytest.mark.asyncio
async def test_grep_cancellation_kills_the_worker(tmp_path: Path, spawned) -> None:
    (tmp_path / "poly.txt").write_text("a" * 1500 + "\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=60)
    task = asyncio.create_task(tool.execute(pattern=r"a*a*a*b"))
    for _ in range(200):
        if spawned:
            break
        await asyncio.sleep(0.05)
    assert spawned
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if all(proc.poll() is not None for _a, _k, proc in spawned):
            break
        await asyncio.sleep(0.05)
    assert all(proc.poll() is not None for _a, _k, proc in spawned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "line", "expect_timeout"),
    [
        (r"(a*)(a*)(a*)b", "a" * 600, True),
        (r"(.*)x(.*)y", "x" * 3000, False),
        (r"a{0,2000}a{0,2000}a{0,2000}b", "a" * 1500, True),
    ],
    ids=["grouped-stars", "grouped-dotstar", "bounded-big"],
)
async def test_grep_grouped_and_bounded_repeats_are_routed_to_the_worker(
    tmp_path: Path, spawned, pattern: str, line: str, expect_timeout: bool
) -> None:
    (tmp_path / "poly.txt").write_text(line + "\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, regex_timeout_s=1)
    max_gap = 0.0
    stop = False

    async def ticker() -> None:
        nonlocal max_gap
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0)
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    result = await asyncio.wait_for(tool.execute(pattern=pattern), timeout=60)
    elapsed = time.monotonic() - started
    stop = True
    await task

    assert len(spawned) == 1
    if expect_timeout:
        assert result.startswith("Error: grep timed out after 1s")
    assert elapsed < 5
    assert max_gap < 0.5
    assert all(proc.poll() is not None for _a, _k, proc in spawned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern", [r"foo.*bar", r"import .* from", r"foo|bar", r"(?i)error", r".*", r"\d{1,3}\.\d{1,3}"]
)
async def test_grep_ordinary_patterns_are_not_routed_to_the_worker(
    tmp_path: Path, spawned, pattern: str
) -> None:
    (tmp_path / "a.txt").write_text("import x from y\nfoo and bar\nError 1.2\n", encoding="utf-8")
    await GrepTool(workspace=tmp_path).execute(pattern=pattern)
    assert spawned == []


@pytest.mark.asyncio
async def test_grep_long_document_line_routes_to_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned
) -> None:
    from nanobot.utils.document import LocatedDocumentLine

    (tmp_path / "report.docx").write_bytes(b"placeholder")

    def fake_source(path, pages=None):
        lines = iter([LocatedDocumentLine("e" * 40_000 + "NEEDLE", 1, "")])
        return SimpleNamespace(lines=lines, continuation=None)

    monkeypatch.setattr("nanobot.agent.tools.search.open_document_line_source", fake_source)
    result = await GrepTool(workspace=tmp_path).execute(
        pattern="NEED[L]E", output_mode="files_with_matches"
    )
    assert result.strip() == "report.docx"
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_grep_worker_batches_lines_and_matches_the_in_process_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned
) -> None:
    monkeypatch.setattr("nanobot.agent.tools.search._GREP_WORKER_BATCH_CHARS", 200)
    text = "".join(f"line {i} \u00e9t\u00e9 {'hit' if i % 7 == 0 else 'miss'}\n" for i in range(300))
    (tmp_path / "b.txt").write_text(text, encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    isolated = await tool.execute(pattern=r".*hit.*\u00e9", context_before=0, context_after=0)
    monkeypatch.setattr("nanobot.agent.tools.search._regex_may_backtrack_badly", lambda *_: False)
    inline = await tool.execute(pattern=r".*hit.*\u00e9", context_before=0, context_after=0)
    assert len(spawned) == 1
    assert isolated == inline
