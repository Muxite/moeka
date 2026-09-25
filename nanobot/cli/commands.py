"""CLI commands for nanobot."""

# pyright: reportConstantRedefinition=false, reportMissingTypeStubs=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false

import os
import sys
from contextlib import suppress
from pathlib import Path

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        with suppress(Exception):
            for stream in (sys.stdout, sys.stderr):
                reconfigure = getattr(stream, "reconfigure", None)
                if callable(reconfigure):
                    reconfigure(encoding="utf-8", errors="replace")

# Keep console encoding setup before importing CLI UI/logging libraries.
import typer  # noqa: E402
from loguru import logger  # noqa: E402

# Remove default handler and re-add with unified nanobot format
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
)


from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.text import Text  # noqa: E402

from nanobot import __logo__, __version__  # noqa: E402
from nanobot.cli import terminal as cli_terminal  # noqa: E402
from nanobot.cli.agent import agent  # noqa: E402
from nanobot.cli.process_identity import set_cli_process_identity  # noqa: E402
from nanobot.cli.provider import provider_app  # noqa: E402
from nanobot.cli.runtime_config import (  # noqa: E402
    _load_inspection_config,
    _load_runtime_config,
    _model_display,
    _print_config_error,
    _print_model_setup_steps,
    _provider_setup_error,
)
from nanobot.utils.helpers import sanitize_surrogates as _sanitize_surrogates  # noqa: E402,F401

# Backward-compatible import for callers that used the former module location.
SafeFileHistory = cli_terminal.SafeFileHistory


app = typer.Typer(
    name="nanobot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} nanobot - Personal AI Assistant",
    epilog=(
        "Run `nanobot` without a subcommand to start the terminal agent. "
        "Use `nanobot agent --help` for agent options."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)

console = Console()

def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanobot - Personal AI Assistant."""
    # Editable/source installs can retain an older generated console script that
    # imports this Typer app directly instead of ``nanobot.cli.entry``. Keep the
    # role identity correct until that launcher is regenerated.
    command = ctx.invoked_subcommand
    set_cli_process_identity([command] if command else ["agent"])
    if command is None:
        from nanobot.cli.entry import _run_agent

        _run_agent([], prog_name="nanobot")


# ============================================================================
# Agent Commands
# ============================================================================


app.command(name="agent")(agent)


# ============================================================================
# Session Commands
# ============================================================================


sessions_app = typer.Typer(help="Manage persisted session history")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("restore-workspace")
def sessions_restore_workspace(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
) -> None:
    """Copy sessions back into the workspace before downgrading nanobot."""
    from nanobot.session.sqlite_store import build_sqlite_session_store

    runtime_config = _load_runtime_config(config, workspace)
    store = build_sqlite_session_store(runtime_config.workspace_path)
    result = store.export_to_workspace()
    console.print(
        f"Restored {result.restored} session file(s) to "
        f"{escape(str(runtime_config.workspace_path / 'sessions'))}; "
        f"{result.unchanged} already matched."
    )
    if result.conflicts:
        console.print(
            "[red]Rollback is incomplete: existing or invalid files require manual review.[/red]"
        )
        for path in result.conflicts:
            console.print(Text(f"- {path}", style="red"))
        raise typer.Exit(1)


@sessions_app.command("migrate")
def sessions_migrate(
    from_path: str = typer.Option(
        ..., "--from", help="Path to the source sessions.db to move"
    ),
    to_path: str = typer.Option(
        ..., "--to", help="Destination path for sessions.db (must not already exist)"
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Report what would move without touching anything (default: on)",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Actually perform the move (required together with --no-dry-run)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Move even if the source database appears to be held open by a "
            "running process. Dangerous: only use this if you are certain "
            "nothing has it open."
        ),
    ),
) -> None:
    """Explicitly move a sessions.db (+ -wal/-shm) from one path to another.

    This is the *only* supported way to relocate session data. Nothing in
    moeka moves a session database automatically anymore: startup only
    detects a legacy in-workspace ``sessions.db`` and warns. See
    .agent/upstream-sync-notes.md's 2026-09-17 incident writeup for why.

    Defaults to a dry run (``--dry-run`` is the default): this prints the
    files it would move, their sizes, and whether the source looks like it
    is currently open by another process, then exits without touching
    anything. Pass both ``--no-dry-run`` and ``--yes`` to actually move the
    files -- neither one alone is enough, so an accidental single flag
    (an unattended script, a fat-fingered flag) can't trigger a real move.
    """
    from nanobot.session.sqlite_store import (
        SessionDbMigrationError,
        migrate_session_database,
        plan_session_database_migration,
    )

    source = Path(from_path).expanduser()
    destination = Path(to_path).expanduser()
    plan = plan_session_database_migration(source, destination)

    if not plan.files:
        console.print(f"[yellow]No session database found at {escape(str(plan.source))}[/yellow]")
        raise typer.Exit(1)

    console.print(f"Source      : {escape(str(plan.source))}")
    console.print(f"Destination : {escape(str(plan.destination))}")
    for label, size in plan.files:
        console.print(f"  - {label}: {size} bytes")
    if plan.destination_exists:
        console.print("[red]Destination already exists -- migration would refuse to run.[/red]")
    if plan.source_in_use:
        console.print(
            "[red]Source appears to be held open by a running process "
            "(a write lock could not be acquired).[/red]"
        )

    if dry_run or not yes:
        console.print(
            "\n[cyan]Dry run only -- nothing was moved.[/cyan] "
            "Re-run with --no-dry-run --yes to perform this migration"
            + (" (add --force to override the in-use check)." if plan.source_in_use else ".")
        )
        return

    try:
        migrate_session_database(source, destination, force=force)
    except SessionDbMigrationError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Migrated[/green] {escape(str(plan.source))} -> {escape(str(plan.destination))}")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show nanobot status."""
    config_path, loaded = _load_inspection_config(config=config, workspace=workspace)
    workspace_path = loaded.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(
        f"Workspace: {workspace_path} "
        f"{'[green]✓[/green]' if workspace_path.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanobot.config.errors import ConfigLoadError
        from nanobot.config.loader import resolve_config_env_vars, resolve_env_refs
        from nanobot.providers.registry import PROVIDERS

        _model, _preset_tag = _model_display(loaded)
        console.print(f"Model: {_model}{_preset_tag}")

        provider_ready = False
        try:
            resolved = resolve_config_env_vars(
                loaded.model_copy(deep=True),
                config_path=config_path,
            )
        except ConfigLoadError as exc:
            console.print("Agent: [red]✗ configuration is not ready[/red]")
            _print_config_error(exc)
        else:
            provider_error = _provider_setup_error(resolved)
            if provider_error:
                console.print(Text(f"Agent: ✗ {provider_error}", style="red"))
                console.print("Complete provider/model setup:")
                _print_model_setup_steps(config_path)
            else:
                provider_ready = True
                console.print("Agent: [green]✓ provider/model configuration is ready[/green]")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(loaded.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if resolve_env_refs(p.api_base or ""):
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(resolve_env_refs(p.api_key or ""))
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")

        if provider_ready:
            console.print()
            console.print('Next: [cyan]nanobot agent -m "Hello!"[/cyan]')
            console.print(
                "[dim]Status does not call the model or verify network access and credentials.[/dim]"
            )
    else:
        console.print("Agent: [red]✗ configuration file not found[/red]")
        console.print("Create the provider/model configuration:")
        _print_model_setup_steps(config_path)


# ============================================================================
# OAuth Login
# ============================================================================

app.add_typer(provider_app, name="provider")


if __name__ == "__main__":
    app()
