"""Tests for allow_patterns priority over deny_patterns."""

from __future__ import annotations

from nanobot.agent.tools.shell import ExecTool


def test_default_posture_is_permissive():
    """Moeka baseline: rm -rf is NOT blocked by default (server-management
    posture — only the fork bomb stays in the default deny list)."""
    tool = ExecTool()
    assert tool._guard_command("rm -rf /tmp/build", "/tmp") is None
    result = tool._guard_command(":(){ :|:& };:", "/tmp")
    assert result is not None
    assert "blocked by safety guard" in result.lower()


def test_allow_patterns_bypass_deny():
    """allow_patterns take priority: matching command skips deny check."""
    tool = ExecTool(allow_patterns=[r"rm\s+-rf\s+/tmp/"])
    result = tool._guard_command("rm -rf /tmp/build", "/tmp")
    assert result is None


def test_allow_patterns_must_match_to_bypass():
    """Non-matching commands are blocked by whitelist-only mode."""
    tool = ExecTool(allow_patterns=[r"rm\s+-rf\s+/opt/"])
    result = tool._guard_command("rm -rf /tmp/build", "/tmp")
    assert result is not None
    assert "allowlist" in result.lower()


def test_extra_deny_patterns_from_config():
    """User deny patterns replace the default layer; internal guards persist."""
    tool = ExecTool(deny_patterns=[r"\bping\b"])
    # ping is blocked by the user deny layer
    assert tool._guard_command("ping example.com", "/tmp") is not None
    # replacing the user layer drops the default fork-bomb guard
    assert tool._guard_command(":(){ :|:& };:", "/tmp") is None
    # non-tunable internal guards (session history) still apply
    assert tool._guard_command("echo x > history.jsonl", "/tmp") is not None


def test_allow_patterns_bypass_extra_deny():
    """allow_patterns also bypasses user-supplied deny patterns."""
    tool = ExecTool(
        deny_patterns=[r"\bping\b"],
        allow_patterns=[r"\bping\s+example\.com\b"],
    )
    result = tool._guard_command("ping example.com", "/tmp")
    assert result is None


def test_allow_patterns_is_whitelist_only():
    """When allow_patterns is set, non-matching non-denied commands are blocked."""
    tool = ExecTool(allow_patterns=[r"\becho\b"])
    # echo matches allow → ok
    assert tool._guard_command("echo hello", "/tmp") is None
    # ls does not match allow and is not in deny → blocked by allowlist
    result = tool._guard_command("ls /tmp", "/tmp")
    assert result is not None
    assert "allowlist" in result.lower()
