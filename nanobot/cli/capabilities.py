"""``nanobot capabilities list|explain|validate|lint|pending|accept|reject``.

Inspection of the typed capability library of the active instance (spec 011 FR-013;
``--workspace`` / ``--config`` honoured). Nothing here calls a model or writes the
instance's capability index: ``explain`` selects over an in-memory index built from
the live skill and tool lists. Spec 010 adds ``lint`` (FR-002) and the review of Dream's
staged skills (FR-001): ``pending`` lists them, ``accept`` and ``reject`` move one; those
two are the only commands here that write (inside the workspace).
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from nanobot.config.schema import Config

capabilities_app = typer.Typer(help="Inspect the typed capability library")

_CONFIG_OPTION = typer.Option(None, "--config", "-c", help="Path to config file")
_WORKSPACE_OPTION = typer.Option(None, "--workspace", "-w", help="Workspace directory")


def _load(config: str | None, workspace: str | None) -> Config:
    """Load config without resolving secrets or printing (stdout stays parseable)."""
    from nanobot.config.errors import ConfigLoadError
    from nanobot.config.loader import load_config

    try:
        loaded = load_config(Path(config).expanduser().resolve() if config else None)
    except (ConfigLoadError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _library(config: Config) -> tuple[Any, list[Any]]:
    """The instance's ``SkillsLoader`` and its built-in tools (no model, no MCP)."""
    from nanobot.agent.skills import SkillsLoader
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.agent.tools.registry import ToolRegistry

    defaults = config.agents.defaults
    workspace = config.workspace_path
    loader = SkillsLoader(
        workspace,
        disabled_skills=set(defaults.disabled_skills) or None,
        allowed_skills=(
            set(defaults.allowed_skills) if defaults.allowed_skills is not None else None
        ),
        lint_mode=config.capabilities.lint,
    )
    registry = ToolRegistry()
    # Tools that need a live loop (spawn, goals, ...) fail to build here; that is
    # expected for an inspection command, so the loader's error log is muted.
    logger.disable("nanobot.agent.tools.loader")
    try:
        ToolLoader().load(
            ToolContext(config=config.tools, workspace=str(workspace), host_tools=True),
            registry,
            allow=defaults.tools_allow,
            deny=defaults.tools_deny,
        )
    finally:
        logger.enable("nanobot.agent.tools.loader")
    tools = [tool for name in registry.tool_names if (tool := registry.get(name)) is not None]
    return loader, tools


def _entries(config: Config) -> list[Any]:
    return _entries_and_lint(config)[0]


def _entries_and_lint(config: Config) -> tuple[list[Any], dict[str, list[str]]]:
    """Index entries plus each listed workspace skill's lint codes (spec 010 FR-002)."""
    from nanobot.capabilities.library import skill_entries, tool_entries

    loader, tools = _library(config)
    lint: dict[str, list[str]] = {}
    if config.capabilities.lint != "off":
        for item in loader.list_skills(filter_unavailable=False):
            if item.get("source") == "workspace":
                codes = loader.workspace_skill_lint_codes(Path(item["path"]))
                if codes:
                    lint[item["name"]] = codes
    return skill_entries(loader) + tool_entries(tools), lint


def _lint_text(codes: list[str] | None) -> str:
    return "lint:" + ",".join(codes) if codes else "-"


def _io_text(io: Any) -> str:
    if io is None:
        return "untyped"
    parts = []
    if io.inputs_all:
        parts.append("in=" + ",".join(io.inputs_all))
    if io.inputs_any:
        parts.append("in_any=" + ",".join(io.inputs_any))
    parts.append("out=" + ",".join(io.outputs))
    parts.append("effects=" + ",".join(io.effects))
    return " ".join(parts)


@capabilities_app.command("list")
def capabilities_list(
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Print every capability: kind, name, io (or untyped), available, sha256 prefix, lint.

    The last column is ``lint:<codes>`` for a workspace skill with lint findings
    (``capabilities.lint`` warn mode; spec 010 FR-002), else ``-``.
    """
    entries, lint = _entries_and_lint(_load(config, workspace))
    if as_json:
        typer.echo(json.dumps([
            {
                "kind": entry.kind,
                "name": entry.name,
                "description": entry.description,
                "io": entry.io.to_dict() if entry.io is not None else "untyped",
                "available": entry.available,
                "content_sha256": entry.content_sha256[:12],
                "lint": lint.get(entry.name, []) if entry.kind == "skill" else [],
            }
            for entry in entries
        ], indent=2, ensure_ascii=False))
        return
    for entry in entries:
        available = "available" if entry.available else "unavailable"
        codes = lint.get(entry.name) if entry.kind == "skill" else None
        typer.echo(
            f"{entry.kind}\t{entry.name}\t{_io_text(entry.io)}\t{available}\t"
            f"{entry.content_sha256[:12]}\t{_lint_text(codes)}"
        )


@capabilities_app.command("explain")
def capabilities_explain(
    message: str = typer.Argument(..., help="The turn's message text"),
    attach: list[str] = typer.Option([], "--attach", help="An attachment file (repeatable)"),
    k: int | None = typer.Option(None, "--k", min=1, max=64, help="Selection size (default top_k)"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Show how a message would be typed and which skills it would select (no model call)."""
    from nanobot.capabilities.index import CapabilityIndex
    from nanobot.capabilities.select import select
    from nanobot.capabilities.task_types import Attachment, extract

    loaded = _load(config, workspace)
    settings = loaded.capabilities
    typing = extract(message, [
        Attachment(name=Path(item).name, mime=mimetypes.guess_type(item)[0]) for item in attach
    ])
    with CapabilityIndex(":memory:") as index:
        index.sync(_entries(loaded))
        selection = select(
            index, typing, message,
            # The gateway and CLI agent are host loops: their tasks have ``host`` too.
            ambient=(*settings.ambient_types, "host"),
            allowed_effects=settings.allowed_effects,
            k=k or settings.top_k,
            kinds=("skill",),
        )
    if as_json:
        typer.echo(json.dumps({
            "task_types": sorted(selection.task_types),
            "requested_output": selection.requested_output,
            "survivors": [
                {"name": name, "score": selection.scores.get(name, 0.0)}
                for name in selection.survivors
            ],
            "selected": list(selection.selected),
            "fallback": selection.fallback,
            "mode": settings.selection,
        }, indent=2, ensure_ascii=False))
        return
    typer.echo(f"task types: {', '.join(sorted(selection.task_types))}")
    typer.echo(f"requested output: {selection.requested_output or '-'}")
    typer.echo(f"survivors ({len(selection.survivors)}):")
    for name in selection.survivors:
        typer.echo(f"  {selection.scores.get(name, 0.0):8.4f}  {name}")
    typer.echo(f"selected ({len(selection.selected)}): {', '.join(selection.selected) or '-'}")
    typer.echo(f"fallback: {selection.fallback or '-'}")
    if settings.selection != "filtered":
        typer.echo("note: capabilities.selection is 'all'; the prompt lists every skill")


@capabilities_app.command("validate")
def capabilities_validate(
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Validate every workspace skill (identity rules and io block); exit 1 if any fails."""
    from nanobot.capabilities.manifest import validate_skill

    root = _load(config, workspace).workspace_path / "skills"
    skill_files = sorted(
        path / "SKILL.md" for path in (root.iterdir() if root.is_dir() else ())
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    invalid = 0
    for skill_file in skill_files:
        problems = validate_skill(skill_file)
        if problems:
            invalid += 1
            typer.echo(f"INVALID {skill_file.parent.name}: " + "; ".join(problems))
    if invalid:
        typer.echo(f"{invalid} of {len(skill_files)} workspace skill(s) invalid")
        raise typer.Exit(1)
    typer.echo(f"{len(skill_files)} workspace skill(s) valid")


@capabilities_app.command("lint")
def capabilities_lint(
    path: str | None = typer.Argument(
        None, help="A skill file or a directory of skills (default: the workspace skills)",
    ),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Lint skill text (spec 010 FR-002); print each finding and exit 1 if there is any."""
    from nanobot.capabilities.lint import lint_file

    if path is not None:
        root = Path(path).expanduser()
        if not root.exists():
            typer.echo(f"Error: {root} does not exist", err=True)
            raise typer.Exit(2)
    else:
        root = _load(config, workspace).workspace_path / "skills"
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted(root.rglob("SKILL.md"))
    else:
        files = []
    total = 0
    for file in files:
        for finding in lint_file(file):
            total += 1
            typer.echo(f"{file}:{finding.line}: {finding.code}: {finding.excerpt}")
    if total:
        typer.echo(f"{total} lint finding(s) in {len(files)} file(s)")
        raise typer.Exit(1)
    typer.echo(f"no lint findings in {len(files)} file(s)")


def _workspace(config: str | None, workspace: str | None) -> Path:
    return _load(config, workspace).workspace_path


@capabilities_app.command("pending")
def capabilities_pending(
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """List Dream's staged skills (skills-pending/) with validation and lint results."""
    from nanobot.capabilities.pending import list_pending

    pending = list_pending(_workspace(config, workspace))
    if as_json:
        typer.echo(json.dumps([
            {
                "name": skill.name,
                "path": str(skill.path),
                "valid": not skill.problems,
                "problems": skill.problems,
                "lint": skill.lint_codes,
                "findings": [
                    {"code": f.code, "line": f.line, "excerpt": f.excerpt}
                    for f in skill.findings
                ],
                "replaces": skill.replaces,
            }
            for skill in pending
        ], indent=2, ensure_ascii=False))
        return
    if not pending:
        typer.echo("no pending skills")
        return
    for skill in pending:
        valid = "valid" if not skill.problems else "invalid: " + "; ".join(skill.problems)
        action = "replaces" if skill.replaces else "new"
        typer.echo(f"{skill.name}\t{action}\t{valid}\t{_lint_text(skill.lint_codes)}")
        for finding in skill.findings:
            typer.echo(f"  line {finding.line}: {finding.code}: {finding.excerpt}")


@capabilities_app.command("accept")
def capabilities_accept(
    name: str = typer.Argument(..., help="The pending skill's name"),
    force_lint: bool = typer.Option(
        False, "--force-lint", help="Accept despite lint findings (validation still applies)",
    ),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Move a pending skill into skills/ if it validates and lints clean; exit 1 otherwise."""
    from nanobot.capabilities.pending import PendingError, accept_pending

    try:
        skill = accept_pending(_workspace(config, workspace), name, force_lint=force_lint)
    except PendingError as exc:
        typer.echo(f"Not accepted: {exc}", err=True)
        raise typer.Exit(1) from exc
    note = " (lint findings accepted: " + ",".join(skill.lint_codes) + ")" if skill.findings else ""
    typer.echo(f"accepted {skill.name}{note}")


@capabilities_app.command("reject")
def capabilities_reject(
    name: str = typer.Argument(..., help="The pending skill's name"),
    config: str | None = _CONFIG_OPTION,
    workspace: str | None = _WORKSPACE_OPTION,
) -> None:
    """Move a pending skill to skills-rejected/<date>/."""
    from nanobot.capabilities.pending import PendingError, reject_pending

    try:
        target = reject_pending(_workspace(config, workspace), name)
    except PendingError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"rejected {name} -> {target}")
