"""Integration tests for the ExecTool — real subprocess execution, no mocks.

These tests actually run shell commands and verify output, exit codes,
timeouts, and file side-effects.  They intentionally do NOT mock subprocess
or any I/O primitive so they catch real environment differences.

Skipped automatically on Windows (Linux/macOS shell semantics expected).
No sudo calls: all commands run as the current user.
"""

from __future__ import annotations

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
async def test_rm_rf_blockable_when_configured() -> None:
    # rm -rf is now permitted by default (server-management agent). The deny
    # filter still works when the operator opts in via config.
    tool = ExecTool(deny_patterns=[r"\brm\s+-[rf]{1,2}\b"])
    output = await _run(tool, "rm -rf /tmp/does_not_exist_integration_test")
    assert any(kw in output.lower() for kw in ["blocked", "denied", "not allowed", "dangerous"]), (
        f"rm -rf should be blocked when deny_patterns configured, got: {output!r}"
    )


@pytest.mark.asyncio
async def test_shutdown_blockable_when_configured() -> None:
    tool = ExecTool(deny_patterns=[r"\b(shutdown|reboot|poweroff)\b"])
    output = await _run(tool, "shutdown -h now")
    assert any(kw in output.lower() for kw in ["blocked", "denied", "not allowed", "dangerous"])


# ---------------------------------------------------------------------------
# allow_patterns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allow_patterns_restricts_to_whitelist() -> None:
    """When allow_patterns is set, only matching commands run.

    Patterns are matched with re.fullmatch against each top-level shell
    segment (not re.search against the whole command) so a chained command
    like "echo hi; rm -rf /" can't slip a disallowed segment through by
    matching only the first one — so the pattern must cover the full segment
    text, not just a prefix.
    """
    tool = ExecTool(allow_patterns=[r"^echo\b.*"])
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


# ---------------------------------------------------------------------------
# bounded one-shot output capture (P0.8): a flood must not be buffered in RAM
# ---------------------------------------------------------------------------

_needs_proc = pytest.mark.skipif(
    not Path("/proc/self/status").exists(), reason="/proc RSS sampling required"
)
_CAPPED = "bytes discarded after the capture limit"
_MIB = 1024 * 1024


def _vmrss_kb() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("VmRSS missing from /proc/self/status")


async def _run_measuring_rss(tool: ExecTool, cmd: str) -> tuple[str, float]:
    """Run *cmd* and return (output, peak RSS growth of this process in MiB).

    VmRSS is sampled every 10 ms while the command runs (so an earlier peak in
    the same pytest process cannot hide the growth, unlike ru_maxrss alone),
    and the ru_maxrss delta is folded in to catch a short spike between samples.
    """
    import asyncio
    import gc
    import resource

    gc.collect()
    base_kb = _vmrss_kb()
    base_max_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_kb = base_kb
    done = asyncio.Event()

    async def _sample() -> None:
        nonlocal peak_kb
        while not done.is_set():
            peak_kb = max(peak_kb, _vmrss_kb())
            await asyncio.sleep(0.01)

    sampler = asyncio.create_task(_sample())
    try:
        output = await tool.execute(cmd)
    finally:
        done.set()
        await sampler
    peak_kb = max(peak_kb, _vmrss_kb())
    maxrss_delta_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base_max_kb
    growth_kb = max(peak_kb - base_kb, maxrss_delta_kb)
    return output, growth_kb / 1024


def _live_pids_with(marker: str) -> list[int]:
    """PIDs (not zombies) whose command line contains *marker*."""
    import os

    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if marker.encode() in cmdline and state != "Z":
            found.append(int(entry.name))
    return found


def _old_format(stdout: str, max_len: int = 10_000) -> str:
    """The pre-cap one-shot format for a stdout-only command exiting 0."""
    result = f"{stdout}\n\nExit code: 0"
    if len(result) > max_len:
        half = max_len // 2
        result = (
            result[:half]
            + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
            + result[-half:]
        )
    return result


@_needs_proc
@pytest.mark.asyncio
async def test_stdout_flood_is_capped_and_memory_bounded() -> None:
    output, growth_mib = await _run_measuring_rss(
        _tool(timeout=60), "head -c 300000000 /dev/zero | tr '\\0' x"
    )
    assert growth_mib < 150, f"exec buffered the flood: RSS grew {growth_mib:.0f} MiB"
    assert "chars truncated" in output
    assert f"... ({300_000_000 - 4 * _MIB:,} {_CAPPED}) ..." in output
    assert output.endswith("x" * 100 + "\n\nExit code: 0")
    assert len(output) < 12_000


@_needs_proc
@pytest.mark.asyncio
async def test_stderr_flood_is_capped_and_memory_bounded() -> None:
    output, growth_mib = await _run_measuring_rss(
        _tool(timeout=60), "head -c 300000000 /dev/zero | tr '\\0' x >&2"
    )
    assert growth_mib < 150, f"exec buffered the flood: RSS grew {growth_mib:.0f} MiB"
    assert output.startswith("STDERR:\nxxxx")
    assert "chars truncated" in output
    assert f"... ({300_000_000 - 4 * _MIB:,} {_CAPPED}) ..." in output
    assert output.endswith("\n\nExit code: 0")


@_needs_proc
@pytest.mark.asyncio
async def test_both_streams_flooding_at_once_is_bounded() -> None:
    cmd = (
        "head -c 200000000 /dev/zero | tr '\\0' x & "
        "head -c 200000000 /dev/zero | tr '\\0' y >&2; wait"
    )
    output, growth_mib = await _run_measuring_rss(_tool(timeout=60), cmd)
    assert growth_mib < 150, f"exec buffered the flood: RSS grew {growth_mib:.0f} MiB"
    assert output.startswith("xxxx")
    assert output.endswith("y" * 100 + "\n\nExit code: 0")
    assert f"... ({2 * (200_000_000 - 4 * _MIB):,} {_CAPPED}) ..." in output


@_needs_proc
@pytest.mark.asyncio
async def test_timeout_still_kills_a_flooding_process_group() -> None:
    import uuid

    marker = f"moekaflood{uuid.uuid4().hex}"
    output, growth_mib = await _run_measuring_rss(_tool(timeout=2), f"yes {marker} | cat")
    assert output == "Error: Command timed out after 2 seconds"
    assert growth_mib < 150, f"exec buffered the flood: RSS grew {growth_mib:.0f} MiB"
    assert _live_pids_with(marker) == []


@pytest.mark.asyncio
async def test_ordinary_output_format_is_unchanged() -> None:
    tool = _tool()
    assert await tool.execute("echo ok") == "ok\n\n\nExit code: 0"
    assert await tool.execute("printf 'line1\\nline2\\nline3\\n'") == (
        "line1\nline2\nline3\n\n\nExit code: 0"
    )
    assert await tool.execute("printf 'a\\nb\\n'; echo boom >&2; exit 3") == (
        "a\nb\n\nSTDERR:\nboom\n\n\nExit code: 3"
    )
    assert await tool.execute("printf '\\377\\376ok'") == "��ok\n\nExit code: 0"
    assert await tool.execute("true") == "\nExit code: 0"
    ls = await tool.execute("ls /nonexistent")
    assert ls.startswith("STDERR:\nls: ") and "/nonexistent" in ls
    assert ls.endswith("\n\n\nExit code: 2")


@pytest.mark.parametrize(
    ("size", "kwargs"),
    [(3_000_000, {}), (5_000_000, {"max_capture_bytes": 8 * _MIB})],
)
@pytest.mark.asyncio
async def test_output_under_the_capture_cap_keeps_the_standard_truncation(
    size: int, kwargs: dict
) -> None:
    output = await _tool(**kwargs).execute(f"head -c {size} /dev/zero | tr '\\0' x")
    assert _CAPPED not in output
    assert output == _old_format("x" * size)


@pytest.mark.asyncio
async def test_capped_output_is_cut_on_utf8_boundaries() -> None:
    # 3,000,000 x U+20AC (3 bytes each): a 2 MiB head/tail split lands mid-char.
    cmd = (
        "python3 -c \"import sys; "
        "sys.stdout.buffer.write('\\u20ac'.encode('utf-8') * 3000000)\""
    )
    output = await _tool().execute(cmd)
    assert "�" not in output
    assert output.startswith("€" * 100)
    # 9,000,000 bytes minus a 2 MiB head (-2 bytes) and 2 MiB tail (-2 bytes).
    assert f"... ({9_000_000 - 4 * _MIB + 4:,} {_CAPPED}) ..." in output


@pytest.mark.asyncio
async def test_capture_cap_is_never_below_four_times_the_output_limit() -> None:
    output = await _tool(max_capture_bytes=10).execute("head -c 100000 /dev/zero | tr '\\0' x")
    # Effective cap: max(10, 4 * 10_000) = 40,000 bytes.
    assert f"... ({100_000 - 40_000:,} {_CAPPED}) ..." in output
    assert "chars truncated" in output
