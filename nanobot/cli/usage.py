"""``nanobot usage``: read-only views of the instance's LLM usage store (spec 009)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import typer

usage_app = typer.Typer(help="Inspect recorded LLM usage")

_HEADER = ("key_fp", "calls", "cost_usd", "first_call_utc", "last_call_utc")


def _usage_db_path(config: str | None, workspace: str | None) -> Path:
    """The usage store of the active instance, without creating anything.

    - ``--config F``: the store sits beside ``F`` (the instance data dir).
    - ``--workspace W``: ``W`` is the instance root (flat layout), so ``W/llm_usage.sqlite3``;
      a kernel built from an in-memory config keeps it under ``W/data/`` instead.
    - neither: the active config's directory (``MOEKA_WORKSPACE``, else ``~/.nanobot``).
    """
    from nanobot.llm_usage import USAGE_DB_FILENAME

    if config:
        return Path(config).expanduser().resolve(strict=False).parent / USAGE_DB_FILENAME
    if workspace:
        root = Path(workspace).expanduser().resolve(strict=False)
        primary = root / USAGE_DB_FILENAME
        nested = root / "data" / USAGE_DB_FILENAME
        return nested if not primary.exists() and nested.exists() else primary
    from nanobot.config.loader import get_config_path

    return get_config_path().parent / USAGE_DB_FILENAME


def _utc(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def _key_rows(path: Path) -> list[tuple[str | None, int, float | None, int | None, int | None]]:
    """``(key_fp, calls, cost, first_ms, last_ms)`` per fingerprint, opened read-only.

    A store written before spec 009 has no ``key_fp`` column; its rows all read as ``None``
    (the CLI never migrates a store a running gateway may hold).
    """
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        if "llm_calls" not in tables:
            return []
        columns = {str(r[1]) for r in connection.execute("PRAGMA table_info(llm_calls)")}
        key = "key_fp" if "key_fp" in columns else "NULL"
        rows = connection.execute(
            f"SELECT {key} AS fp, COUNT(*), SUM(cost_usd), COUNT(cost_usd), "  # noqa: S608
            "MIN(started_at_ms), MAX(started_at_ms) FROM llm_calls "
            "GROUP BY fp ORDER BY COUNT(*) DESC, fp"
        ).fetchall()
    finally:
        connection.close()
    return [
        (fp, int(calls), float(cost) if priced else None, first, last)
        for fp, calls, cost, priced, first, last in rows
    ]


@usage_app.command("keys")
def usage_keys(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Instance directory"),
    fingerprint_env: str | None = typer.Option(
        None,
        "--fingerprint-env",
        metavar="VAR",
        help="Also print the fingerprint of environment variable VAR (never its value)",
    ),
) -> None:
    """One line per API-key fingerprint: calls, cost, first and last call (UTC).

    The fingerprint is the first 12 hex characters of sha256 of the key. ``none`` groups
    calls made without a key (OAuth, local) or recorded before fingerprints existed. Cost
    sums the priced calls; ``unknown`` when none of them is priced.
    """
    from nanobot.providers.base import key_fingerprint

    path = _usage_db_path(config, workspace)
    try:
        rows = _key_rows(path)
    except sqlite3.Error as exc:
        typer.echo(f"Error: cannot read usage store {path}: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("  ".join(_HEADER))
    for fp, calls, cost, first, last in rows:
        typer.echo("  ".join((
            fp or "none",
            str(calls),
            "unknown" if cost is None else f"{cost:.6f}",
            _utc(first),
            _utc(last),
        )))
    if fingerprint_env:
        env_fp = key_fingerprint(os.environ.get(fingerprint_env))
        if env_fp is None:
            typer.echo(f"{fingerprint_env}: none (unset or empty)")
        else:
            matched = sum(calls for fp, calls, *_ in rows if fp == env_fp)
            typer.echo(f"{fingerprint_env}: {env_fp} ({matched} calls in this store)")
