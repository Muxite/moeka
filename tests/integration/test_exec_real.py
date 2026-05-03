"""Integration tests for the ExecTool — real subprocess execution, no mocks.

These tests actually run shell commands and verify output, exit codes,
timeouts, and file side-effects.  They intentionally do NOT mock subprocess
or any I/O primitive so they catch real environment differences.

Skipped automatically on Windows (Linux/macOS shell semantics expected).
No sudo calls: all commands run as the current user.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from nanobot.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix shell required")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tool(*, timeout: int = 30, working_dir: str | None = None, **kw) -> ExecTool:
    return ExecTool(timeout=timeout, working_dir=working_dir, **kw)


async def _run(tool: ExecTool, cmd: str, working_dir: str | None = None) -> str:
    """Run a command and return the combined output string."""
    kwargs: dict = {}
    if working_dir is not None:
        kwargs["working_dir"] = working_dir
    return await tool.execute(cmd, **kwargs)


# ---------------------------------------------------------------------------
# basic execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_echo_returns_stdout() -> None:
    output = await _run(_tool(), "echo hello_integration")
    assert "hello_integration" in output


@pytest.mark.asyncio
async def test_multiline_output() -> None:
    output = await _run(_tool(), "printf 'line1\\nline2\\nline3\\n'")
    assert "line1" in output
    assert "line2" in output
    assert "line3" in output


@pytest.mark.asyncio
async def test_exit_code_reflected_in_output() -> None:
    output = await _run(_tool(), "exit 42")
    assert "42" in output  # exit code is appended to output


@pytest.mark.asyncio
async def test_stderr_captured() -> None:
    output = await _run(_tool(), "echo error_msg >&2")
    assert "error_msg" in output


@pytest.mark.asyncio
async def test_pipeline_works() -> None:
    output = await _run(_tool(), "echo 'hello world' | tr '[:lower:]' '[:upper:]'")
    assert "HELLO WORLD" in output


# ---------------------------------------------------------------------------
# file creation and reading
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_command_produces_real_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    await _run(_tool(working_dir=str(tmp_path)), f"echo written > {target}")
    assert target.exists(), "command must create the file"
    assert "written" in target.read_text()


@pytest.mark.asyncio
async def test_command_reads_existing_file(tmp_path: Path) -> None:
    src = tmp_path / "input.txt"
    src.write_text("file_content_42\n")
    output = await _run(_tool(), f"cat {src}")
    assert "file_content_42" in output


@pytest.mark.asyncio
async def test_working_dir_is_respected(tmp_path: Path) -> None:
    output = await _run(_tool(working_dir=str(tmp_path)), "pwd")
    assert str(tmp_path) in output


@pytest.mark.asyncio
async def test_write_and_count_lines(tmp_path: Path) -> None:
    """Write 99 newlines to a file, count with wc -l."""
    target = tmp_path / "lines.txt"
    target.write_text("\n".join(str(j) for j in range(100)))
    output = await _run(_tool(), f"wc -l < {target}")
    # wc -l counts newlines; 100 items with join gives 99 newlines
    assert any(str(n) in output for n in [99, 100])


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_env_present_in_subprocess() -> None:
    output = await _run(_tool(), "echo $HOME")
    # HOME should expand to something containing the actual home dir name
    assert Path.home().name in output or str(Path.home()) in output


@pytest.mark.asyncio
async def test_path_env_allows_finding_echo() -> None:
    output = await _run(_tool(), "which echo")
    assert "echo" in output


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_long_running_command_is_killed() -> None:
    """A command that sleeps longer than the timeout must be terminated."""
    tool = _tool(timeout=2)
    output = await _run(tool, "sleep 60")
    assert any(kw in output.lower() for kw in ["timeout", "timed out", "killed", "signal"]), (
        f"Expected timeout indicator, got: {output!r}"
    )


# ---------------------------------------------------------------------------
# deny-list safety (no real risk commands, just pattern checks)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rm_rf_is_blocked_by_default() -> None:
    output = await _run(_tool(), "rm -rf /tmp/does_not_exist_integration_test")
    assert any(kw in output.lower() for kw in ["blocked", "denied", "not allowed", "error"]), (
        f"rm -rf should be blocked by default, got: {output!r}"
    )


@pytest.mark.asyncio
async def test_shutdown_is_blocked() -> None:
    output = await _run(_tool(), "shutdown -h now")
    assert any(kw in output.lower() for kw in ["blocked", "denied", "not allowed", "error"])


# ---------------------------------------------------------------------------
# allow_patterns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allow_patterns_restricts_to_whitelist() -> None:
    """When allow_patterns is set, only matching commands run."""
    tool = ExecTool(allow_patterns=[r"^echo\b"])
    output_ok = await _run(tool, "echo allowed")
    assert "allowed" in output_ok

    output_blocked = await _run(tool, "ls /tmp")
    assert any(kw in output_blocked.lower() for kw in ["blocked", "denied", "not allowed", "error"]), (
        f"ls should be blocked by allow_patterns=[echo], got: {output_blocked!r}"
    )


# ---------------------------------------------------------------------------
# real process output details
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_python_version_command() -> None:
    """python3 --version produces output and exits 0."""
    output = await _run(_tool(), "python3 --version")
    assert "Python" in output
    assert "Exit code: 0" in output


@pytest.mark.asyncio
async def test_json_processing_pipeline(tmp_path: Path) -> None:
    """Write JSON, parse with python3, verify output — a realistic pipeline."""
    import json
    json_file = tmp_path / "data.json"
    json_file.write_text(json.dumps({"key": "value_123"}))
    output = await _run(
        _tool(),
        f"python3 -c \"import json; d=json.load(open('{json_file}')); print(d['key'])\"",
    )
    assert "value_123" in output


@pytest.mark.asyncio
async def test_env_var_passed_through_with_allowed_env_keys(monkeypatch) -> None:
    """Env vars in allowed_env_keys are forwarded to the subprocess."""
    monkeypatch.setenv("MY_CUSTOM_VAR_TEST", "custom_value_xyz")
    tool = ExecTool(allowed_env_keys=["MY_CUSTOM_VAR_TEST"])
    output = await _run(tool, "echo $MY_CUSTOM_VAR_TEST")
    assert "custom_value_xyz" in output


@pytest.mark.asyncio
async def test_create_and_list_dir(tmp_path: Path) -> None:
    """mkdir + ls round-trip via exec."""
    subdir = tmp_path / "new_dir"
    await _run(_tool(), f"mkdir {subdir}")
    assert subdir.is_dir()
    output = await _run(_tool(), f"ls {tmp_path}")
    assert "new_dir" in output
