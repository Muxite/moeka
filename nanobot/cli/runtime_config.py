"""Configuration loading and diagnostics shared by CLI commands."""

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from nanobot.config.schema import Config

__all__ = [
    "_load_config_for_cli",
    "_load_inspection_config",
    "_load_runtime_config",
    "_model_display",
    "_print_agent_start_error",
    "_print_config_error",
    "_print_model_setup_steps",
    "_provider_setup_error",
]

console = Console()


def _model_display(config: Config) -> tuple[str, str]:
    """Return (resolved_model_name, preset_tag) for display strings."""
    resolved = config.resolve_preset()
    name = config.agents.defaults.model_preset
    tag = f" (preset: {name})" if name else ""
    return resolved.model, tag


def _print_config_error(error: Exception) -> None:
    """Render a configuration failure without exposing traceback internals."""
    from nanobot.config.errors import ConfigLoadError

    console.print(Text(str(error), style="red"))
    if isinstance(error, ConfigLoadError):
        command = _status_command(error.path)
        console.print(f"[dim]Check again after editing: {escape(command)}[/dim]")


def _status_command(config_path: Path) -> str:
    return f'nanobot status --config "{config_path}"'


def _print_model_setup_steps(config_path: Path) -> None:
    """Show the shortest setup routes shared by Status and Agent startup."""
    console.print(
        f"  Edit:  [cyan]{escape(str(config_path))}[/cyan] and set "
        "agents.defaults.model plus a providers.<name>.apiKey"
    )
    console.print("  OAuth: run [cyan]nanobot provider login <provider>[/cyan]")
    console.print(f"  Check: [cyan]{escape(_status_command(config_path))}[/cyan]")


def _print_agent_start_error(error: ValueError) -> None:
    from nanobot.config.loader import get_config_path

    console.print(Text(f"Agent cannot start: {error}", style="red"))
    console.print("Complete provider/model setup:")
    _print_model_setup_steps(get_config_path())


def _load_config_for_cli(
    config_path: Path | None = None,
    *,
    resolve_env: bool = False,
) -> Config:
    """Load CLI configuration and turn expected failures into a clean exit."""
    from nanobot.config.errors import ConfigLoadError
    from nanobot.config.loader import load_config, resolve_config_env_vars

    try:
        loaded = load_config(config_path)
        if resolve_env:
            loaded = resolve_config_env_vars(loaded)
        return loaded
    except ConfigLoadError as exc:
        _print_config_error(exc)
        raise typer.Exit(1) from exc


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from nanobot.config.loader import set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = _load_config_for_cli(config_path, resolve_env=True)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _load_inspection_config(
    config: str | None = None,
    workspace: str | None = None,
) -> tuple[Path, Config]:
    """Load config for diagnostic commands without resolving secret env refs."""
    from nanobot.config.errors import ConfigLoadError
    from nanobot.config.loader import get_config_path, load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve(strict=False)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    display_path = config_path or get_config_path()
    try:
        loaded = load_config(config_path)
    except ConfigLoadError as exc:
        _print_config_error(exc)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return display_path, loaded


def _provider_setup_error(config: Config) -> str | None:
    """Return a local provider/model configuration error, or None."""
    from nanobot.providers.factory import validate_provider_setup

    try:
        validate_provider_setup(config)
    except ValueError as exc:
        return str(exc)
    return None
