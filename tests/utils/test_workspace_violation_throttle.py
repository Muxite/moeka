"""Tests for repeated_workspace_violation throttle and signature."""

from __future__ import annotations

from nanobot.utils.runtime import (
    exec_guard_violation_signature,
    repeated_exec_guard_error,
    repeated_workspace_violation_error,
    workspace_violation_signature,
)


def test_signature_for_filesystem_tools_uses_path_argument():
    sig_a = workspace_violation_signature(
        "read_file", {"path": "/Users/x/Downloads/01.md"}
    )
    sig_b = workspace_violation_signature(
        "write_file", {"path": "/Users/x/Downloads/01.md"}
    )
    sig_c = workspace_violation_signature(
        "edit_file", {"file_path": "/Users/x/Downloads/01.md"}
    )

    assert sig_a is not None
    assert sig_a == sig_b == sig_c, (
        "the throttle must collapse equivalent paths across different tools "
        "so the LLM cannot bypass it by switching tool"
    )
    assert "/users/x/downloads/01.md" in sig_a


def test_signature_for_exec_extracts_first_absolute_path_in_command():
    sig = workspace_violation_signature(
        "exec",
        {"command": "cat /Users/x/Downloads/01.md && echo done"},
    )
    assert sig is not None
    assert "/users/x/downloads/01.md" in sig


def test_signature_collides_across_filesystem_and_exec_for_same_target():
    """LLM bypass loops jump tools (read_file -> exec cat). Throttle must
    treat both attempts as targeting the same outside resource."""
    fs_sig = workspace_violation_signature(
        "read_file", {"path": "/Users/x/Downloads/01.md"}
    )
    exec_sig = workspace_violation_signature(
        "exec", {"command": "cat /Users/x/Downloads/01.md"}
    )
    assert fs_sig == exec_sig


def test_signature_falls_back_to_working_dir_when_no_absolute_in_command():
    sig = workspace_violation_signature(
        "exec",
        {"command": "ls -la", "working_dir": "/etc"},
    )
    assert sig is not None
    assert "/etc" in sig


def test_signature_is_none_for_unknown_tool_with_no_path():
    assert workspace_violation_signature("web_search", {"query": "anything"}) is None
    assert workspace_violation_signature("exec", {"command": "echo hello"}) is None


def test_repeated_workspace_violation_returns_none_within_budget():
    counts: dict[str, int] = {}
    arguments = {"path": "/Users/x/Downloads/01.md"}

    assert repeated_workspace_violation_error("read_file", arguments, counts) is None
    assert repeated_workspace_violation_error("read_file", arguments, counts) is None


def test_repeated_workspace_violation_escalates_after_third_attempt():
    counts: dict[str, int] = {}
    arguments = {"path": "/Users/x/Downloads/01.md"}

    repeated_workspace_violation_error("read_file", arguments, counts)
    repeated_workspace_violation_error("read_file", arguments, counts)
    third = repeated_workspace_violation_error("read_file", arguments, counts)

    assert third is not None
    assert "refusing repeated workspace-bypass" in third
    assert "/users/x/downloads/01.md" in third
    assert "ask how they want to proceed" in third


def test_repeated_workspace_violation_independent_per_target():
    """Different outside paths must each get their own retry budget."""
    counts: dict[str, int] = {}

    repeated_workspace_violation_error(
        "read_file", {"path": "/Users/x/Downloads/01.md"}, counts,
    )
    repeated_workspace_violation_error(
        "read_file", {"path": "/Users/x/Downloads/01.md"}, counts,
    )
    # Different target, fresh budget.
    assert repeated_workspace_violation_error(
        "read_file", {"path": "/Users/x/Documents/notes.md"}, counts,
    ) is None


def test_repeated_workspace_violation_collapses_tool_switching():
    """LLM switches from read_file to exec cat then to python -c open(...)
    against the same path; the throttle must escalate on the third attempt."""
    counts: dict[str, int] = {}

    repeated_workspace_violation_error(
        "read_file", {"path": "/Users/x/Downloads/01.md"}, counts,
    )
    repeated_workspace_violation_error(
        "exec", {"command": "cat /Users/x/Downloads/01.md"}, counts,
    )
    third = repeated_workspace_violation_error(
        "exec",
        {"command": "python3 -c \"open('/Users/x/Downloads/01.md').read()\""},
        counts,
    )
    assert third is not None
    assert "refusing repeated workspace-bypass" in third


# --- exec command-guard denials (allowlist / deny patterns) ---

_ALLOWLIST_DENIAL = (
    "Error: Command blocked by allowlist filter. exec is in whitelist-only mode: "
    "tools.exec.allow_patterns is non-empty (5 pattern(s)), so ONLY commands "
    "matching those patterns may run."
)
_DENYGUARD_DENIAL = (
    "Error: Command blocked by safety guard (dangerous pattern detected). "
    "Matched '\\\\b(mkfs|diskpart)\\\\b'. "
    "Adjust tools.exec.deny_patterns in config to permit it."
)


def test_exec_guard_signature_classifies_allowlist_and_denyguard():
    assert exec_guard_violation_signature(_ALLOWLIST_DENIAL) == "violation:exec-allowlist"
    assert exec_guard_violation_signature(_DENYGUARD_DENIAL) == "violation:exec-denyguard"


def test_exec_guard_signature_is_none_for_unrelated_errors():
    assert exec_guard_violation_signature("") is None
    assert exec_guard_violation_signature("Error: command not found: foo") is None
    assert exec_guard_violation_signature(
        "Error: Path is outside the configured workspace"
    ) is None


def test_repeated_exec_guard_escalates_after_third_denial():
    """The LLM retries a *different* command each time, so the throttle must
    key on the denial class, not the command/path."""
    counts: dict[str, int] = {}

    assert repeated_exec_guard_error(_ALLOWLIST_DENIAL, counts) is None
    assert repeated_exec_guard_error(_ALLOWLIST_DENIAL, counts) is None
    third = repeated_exec_guard_error(_ALLOWLIST_DENIAL, counts)

    assert third is not None
    assert "refusing repeated exec attempts" in third
    assert "allow_patterns" in third
    assert "Stop retrying" in third


def test_repeated_exec_guard_denyguard_message_names_deny_patterns():
    counts: dict[str, int] = {}

    repeated_exec_guard_error(_DENYGUARD_DENIAL, counts)
    repeated_exec_guard_error(_DENYGUARD_DENIAL, counts)
    third = repeated_exec_guard_error(_DENYGUARD_DENIAL, counts)

    assert third is not None
    assert "deny_patterns" in third


def test_repeated_exec_guard_independent_budgets_per_class():
    counts: dict[str, int] = {}

    repeated_exec_guard_error(_ALLOWLIST_DENIAL, counts)
    repeated_exec_guard_error(_ALLOWLIST_DENIAL, counts)
    # Different denial class, fresh budget.
    assert repeated_exec_guard_error(_DENYGUARD_DENIAL, counts) is None
