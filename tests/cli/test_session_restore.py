from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli import commands
from nanobot.config.loader import load_config
from nanobot.session.manager import SessionManager
from nanobot.session.sqlite_store import SqliteSessionStore, default_sessions_root


def test_sessions_restore_workspace_command_prepares_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "instance" / "config.json"
    config = load_config(config_path)
    config.agents.defaults.workspace = str(workspace)
    # Must match what nanobot sessions restore-workspace computes internally
    # (nanobot/cli/commands.py: default_sessions_root(workspace_path)).
    sessions_root = default_sessions_root(workspace)
    manager = SessionManager(
        workspace,
        sessions_root=sessions_root,
        store=SqliteSessionStore(workspace, sessions_root=sessions_root),
    )
    session = manager.get_or_create("cli:rollback")
    session.add_message("user", "restore-me")
    manager.save(session, fsync=True)
    monkeypatch.setattr(commands, "_load_runtime_config", lambda *_args: config)

    result = CliRunner().invoke(commands.app, ["sessions", "restore-workspace"])

    assert result.exit_code == 0, result.output
    assert "Restored 1 session file(s)" in result.output
    restored = workspace / "sessions" / f"{manager.safe_key(session.key)}.jsonl"
    assert restored.exists()
    assert "restore-me" in restored.read_text(encoding="utf-8")
    # The SQLite store itself (sessions_root, not the workspace) still has it.
    assert manager._load(session.key) is not None
