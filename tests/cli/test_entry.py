import os
import subprocess
import sys
from pathlib import Path

from nanobot.cli import entry
from nanobot.cli.entry import _agent_invocation_args


def test_root_command_routes_to_agent_without_copying_agent_options() -> None:
    assert _agent_invocation_args([]) == []
    assert _agent_invocation_args(["agent", "--logs"]) == ["--logs"]
    assert _agent_invocation_args(["--workspace", "./project"]) == [
        "--workspace",
        "./project",
    ]
    assert _agent_invocation_args(["-mhello"]) == ["-mhello"]


def test_root_metadata_and_subcommands_keep_the_root_cli() -> None:
    for args in (
        ["--help"],
        ["--version"],
        ["--install-completion"],
        ["status"],
        ["sessions"],
        ["provider"],
    ):
        assert _agent_invocation_args(args) is None


def test_root_shell_completion_keeps_root_subcommands() -> None:
    env = os.environ.copy()
    env.update(
        {
            "_NANOBOT_COMPLETE": "complete_bash",
            "COMP_WORDS": "nanobot ",
            "COMP_CWORD": "1",
        }
    )
    script = (
        "import sys; "
        "from nanobot.cli.entry import main; "
        "sys.argv = ['nanobot']; "
        "main()"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    completions = set(result.stdout.splitlines())
    assert {"agent", "status", "sessions", "provider"} <= completions
    assert not {"gateway", "webui", "onboard", "channels", "serve"} & completions
    assert "not supported" not in result.stderr


def test_root_alias_dispatches_the_shared_agent_command(monkeypatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(entry.sys, "argv", ["nanobot", "-m", "hello"])
    monkeypatch.setattr(
        entry,
        "set_cli_process_identity",
        lambda args: calls.__setitem__("identity", args),
    )
    monkeypatch.setattr(entry, "_configure_windows_console", lambda: None)
    monkeypatch.setattr(
        entry,
        "_run_agent",
        lambda args, *, prog_name: calls.update(args=args, prog_name=prog_name),
    )

    entry.main()

    assert calls == {
        "identity": ["agent", "-m", "hello"],
        "args": ["-m", "hello"],
        "prog_name": "nanobot",
    }


def _capture_dispatch(monkeypatch, argv: list[str]) -> dict[str, object]:
    calls: dict[str, object] = {}
    monkeypatch.setattr(entry.sys, "argv", argv)
    monkeypatch.setattr(entry, "set_cli_process_identity", lambda _args: None)
    monkeypatch.setattr(entry, "_configure_windows_console", lambda: None)
    monkeypatch.setattr(
        entry,
        "_run_agent",
        lambda args, *, prog_name: calls.update(args=args, prog_name=prog_name),
    )
    entry.main()
    return calls


def test_bare_invocation_starts_the_agent(monkeypatch) -> None:
    assert _capture_dispatch(monkeypatch, ["nanobot"]) == {"args": [], "prog_name": "nanobot"}


def test_agent_subcommand_goes_through_the_full_cli(monkeypatch) -> None:
    launched: list[bool] = []
    monkeypatch.setattr("nanobot.cli.commands.app", lambda: launched.append(True))
    for argv in (
        ["nanobot", "agent"],
        ["nanobot", "agent", "-m", "hello"],
        ["nanobot", "agent", "--classic"],
    ):
        assert _capture_dispatch(monkeypatch, argv) == {}
    assert launched == [True, True, True]
