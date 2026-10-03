"""Spec 009 FR-008: ``nanobot usage keys`` lists calls per API-key fingerprint."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import LLMUsageStore

runner = CliRunner()
KEY_A = "sk-or-v1-aaaaFAKEaaaa0000111122223333444455556666"
KEY_B = "sk-or-v1-bbbbFAKEbbbb7777888899990000aaaabbbbcccc"
_T0 = (int(time.time()) - 3600) * 1000  # whole seconds, inside the retention window


def _fp(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def _populate(root: Path) -> None:
    store = LLMUsageStore(root / "llm_usage.sqlite3")
    rows = [(_fp(KEY_A), 0.5, 0), (_fp(KEY_A), 0.25, 60_000), (_fp(KEY_B), None, 5_000),
            (None, 0.1, 9_000)]
    for key_fp, cost, offset in rows:
        store.record(LLMCallRecord(
            started_at_ms=_T0 + offset, duration_ms=1, provider="openrouter",
            model="openai/gpt-6-luna", source="api", stream=False, finish_reason="stop",
            cost_usd=cost, key_fp=key_fp,
        ))
    store.close()


def _lines(output: str) -> dict[str, list[str]]:
    rows = [line.split() for line in output.strip().splitlines()]
    return {row[0]: row for row in rows[1:] if row and ":" not in row[0]}


def test_lists_one_line_per_fingerprint(tmp_path: Path) -> None:
    _populate(tmp_path)
    result = runner.invoke(app, ["usage", "keys", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].split()[0] == "key_fp"
    rows = _lines(result.output)
    assert set(rows) == {_fp(KEY_A), _fp(KEY_B), "none"}
    assert rows[_fp(KEY_A)][1:] == ["2", "0.750000", _utc(_T0), _utc(_T0 + 60_000)]
    assert rows[_fp(KEY_B)][1:3] == ["1", "unknown"]
    assert rows["none"][1:3] == ["1", "0.100000"]


def test_empty_store_prints_the_header_only(tmp_path: Path) -> None:
    result = runner.invoke(app, ["usage", "keys", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(result.output.strip().splitlines()) == 1
    assert not (tmp_path / "llm_usage.sqlite3").exists()  # reading creates nothing


def test_fingerprint_env_prints_the_fingerprint_never_the_value(tmp_path: Path) -> None:
    _populate(tmp_path)
    result = runner.invoke(
        app, ["usage", "keys", "--workspace", str(tmp_path), "--fingerprint-env", "LUNA_KEY"],
        env={"LUNA_KEY": f" {KEY_A}\n"},
    )
    assert result.exit_code == 0, result.output
    assert f"LUNA_KEY: {_fp(KEY_A)}" in result.output
    for i in range(len(KEY_A) - 7):
        assert KEY_A[i:i + 8] not in result.output

    unset = runner.invoke(
        app, ["usage", "keys", "--workspace", str(tmp_path), "--fingerprint-env", "NO_SUCH_VAR"],
        env={"NO_SUCH_VAR": ""},
    )
    assert unset.exit_code == 0 and "NO_SUCH_VAR: none" in unset.output


def test_pre_009_store_reads_as_none_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, started_at_ms INTEGER, "
            "cost_usd REAL)"
        )
        connection.execute("INSERT INTO llm_calls (started_at_ms, cost_usd) VALUES (?, NULL)",
                           (_T0,))
    result = runner.invoke(app, ["usage", "keys", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert _lines(result.output)["none"][1:3] == ["1", "unknown"]
    with closing(sqlite3.connect(path)) as connection:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(llm_calls)")}
    assert "key_fp" not in columns


def test_module_entry_point(tmp_path: Path) -> None:
    _populate(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "nanobot", "usage", "keys", "--workspace", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert _fp(KEY_A) in proc.stdout
