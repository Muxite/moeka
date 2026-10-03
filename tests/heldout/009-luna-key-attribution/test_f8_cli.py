"""FR-008: ``nanobot usage keys`` (one line per key_fp) and ``--fingerprint-env VAR``.

Instance layout: ``inst/`` holds ``config.json`` (workspace = ``inst``), ``MOEKA_WORKSPACE=inst``
and ``--workspace inst`` are both given, and the pre-populated store is written to both
candidate data dirs (``inst/`` = the config's directory, and ``inst/data/`` = a config-less
kernel's ``state_dir/data``) with identical rows, so either reading of "the active instance"
sees the same store.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _h009 import (
    DB_NAME,
    FP_TOKEN,
    KEY_AWORK,
    KEY_MOEKA,
    KEY_THIRD,
    leaks,
    now_ms,
    record,
    ref_fp,
    repo_root,
)

FP1, FP2, FP3 = ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK), ref_fp(KEY_THIRD)
pytestmark = pytest.mark.timeout(120)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _instance(tmp_path: Path) -> Path:
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.json").write_text(json.dumps(
        {"agents": {"defaults": {"workspace": str(inst)}}}), encoding="utf-8")
    return inst


def _populate(inst: Path, rows: list) -> None:
    from nanobot.llm_usage.store import LLMUsageStore

    for data_dir in (inst, inst / "data"):
        s = LLMUsageStore(data_dir / DB_NAME)
        try:
            s.count()
            for rec in rows:
                s.record(rec)
        finally:
            s.close()


def _run(inst: Path, *args: str, extra_env: dict | None = None, home: Path | None = None):
    repo = repo_root()
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    env.update({
        "HOME": str(home or inst.parent / "home"),
        "MOEKA_WORKSPACE": str(inst),
        "PYTHONPATH": str(repo),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "250",
    })
    env.update(extra_env or {})
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "nanobot", "usage", "keys", "--workspace", str(inst), *args],
        cwd=inst.parent, env=env, capture_output=True, text=True, timeout=100,
    )


def _lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.strip()]


def _floats(line: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?<![\w.])\$?(\d+\.\d+)", line.replace("$", ""))]


def _ints(line: str) -> list[int]:
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?", " ", line)
    stripped = FP_TOKEN.sub(" ", stripped)
    return [int(x) for x in re.findall(r"(?<![\w.])(\d+)(?![\w.])", stripped)]


def _populated(tmp_path):
    """Instance with fp1 x3, fp2 x2, keyless x1, fp3 x1 (unpriced). Built inside each test so a
    missing feature is a test failure, not a fixture error."""
    inst = _instance(tmp_path)
    t0 = now_ms(-86400)
    stamps = {
        FP1: [t0, t0 + 60_000, t0 + 3_600_000],
        FP2: [t0 + 120_000, t0 + 7_200_000],
        None: [t0 + 30_000],
        FP3: [t0 + 45_000],
    }
    costs = {FP1: [0.10, 0.25, 0.40], FP2: [0.05, 0.15], None: [0.30], FP3: [None]}
    rows = []
    for fp, ts_list in stamps.items():
        for ts, cost in zip(ts_list, costs[fp], strict=True):
            rows.append(record(key_fp=fp, started_at_ms=ts, cost_usd=cost))
    _populate(inst, rows)
    return inst, stamps


@pytest.mark.fr("FR-008")
def test_exit_zero_and_one_line_per_fp(tmp_path):
    inst, _ = _populated(tmp_path)
    proc = _run(inst)
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = _lines(proc.stdout)
    for fp in (FP1, FP2, FP3):
        assert sum(fp in ln for ln in lines) == 1, fp
    none_lines = [ln for ln in lines if re.search(r"(?<![\w-])none(?![\w-])", ln, re.I)
                  and not FP_TOKEN.search(ln)]
    assert len(none_lines) == 1


@pytest.mark.fr("FR-008")
def test_counts_and_costs(tmp_path):
    inst, _ = _populated(tmp_path)
    lines = _lines(_run(inst).stdout)
    line1 = next(ln for ln in lines if FP1 in ln)
    line2 = next(ln for ln in lines if FP2 in ln)
    assert 3 in _ints(line1)
    assert any(abs(f - 0.75) < 1e-6 for f in _floats(line1)), line1
    assert 2 in _ints(line2)
    assert any(abs(f - 0.20) < 1e-6 for f in _floats(line2)), line2


@pytest.mark.fr("FR-008")
def test_none_row_counts_keyless_calls(tmp_path):
    inst, _ = _populated(tmp_path)
    lines = _lines(_run(inst).stdout)
    none_line = next(ln for ln in lines if re.search(r"(?<![\w-])none(?![\w-])", ln, re.I)
                     and not FP_TOKEN.search(ln))
    assert 1 in _ints(none_line)
    assert any(abs(f - 0.30) < 1e-6 for f in _floats(none_line)), none_line


@pytest.mark.fr("FR-008")
def test_unpriced_cost_is_unknown(tmp_path):
    inst, _ = _populated(tmp_path)
    line3 = next(ln for ln in _lines(_run(inst).stdout) if FP3 in ln)
    assert "unknown" in line3.lower()
    assert not any(abs(f) < 1e-12 for f in _floats(line3)), "unknown cost printed as 0"


@pytest.mark.fr("FR-008")
def test_first_and_last_call_times_utc_iso(tmp_path):
    inst, stamps = _populated(tmp_path)
    lines = _lines(_run(inst).stdout)
    for fp in (FP1, FP2):
        line = next(ln for ln in lines if fp in ln)
        first, last = _iso(min(stamps[fp])), _iso(max(stamps[fp]))
        assert first in line, (fp, line)
        assert last in line, (fp, line)
        assert line.index(first) < line.index(last)
    line3 = next(ln for ln in lines if FP3 in ln)
    assert line3.count(_iso(stamps[FP3][0])) >= 1


@pytest.mark.fr("FR-008")
def test_has_a_header(tmp_path):
    inst, _ = _populated(tmp_path)
    lines = _lines(_run(inst).stdout)
    first_data = min(i for i, ln in enumerate(lines) if FP_TOKEN.search(ln))
    assert first_data >= 1, "no header line before the rows"


@pytest.mark.fr("FR-008")
def test_empty_store_prints_header_only(tmp_path):
    inst = _instance(tmp_path)
    _populate(inst, [])
    proc = _run(inst)
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = _lines(proc.stdout)
    assert len(lines) >= 1
    assert not any(FP_TOKEN.search(ln) for ln in lines)
    assert not any(re.search(r"(?<![\w-])none(?![\w-])", ln, re.I)
                   and re.search(r"\d{4}-\d{2}-\d{2}T", ln) for ln in lines)


@pytest.mark.fr("FR-008")
def test_missing_store_file_is_empty_not_an_error(tmp_path):
    inst = _instance(tmp_path)
    proc = _run(inst)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Traceback" not in proc.stderr
    assert not any(FP_TOKEN.search(ln) for ln in _lines(proc.stdout))


@pytest.mark.fr("FR-008", "FR-006")
def test_fingerprint_env_prints_fp_never_value(tmp_path):
    inst, _ = _populated(tmp_path)
    proc = _run(inst, "--fingerprint-env", "H009_KEY", extra_env={"H009_KEY": KEY_MOEKA})
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout + proc.stderr
    assert FP1 in proc.stdout
    assert leaks(out, KEY_MOEKA) == []
    assert "H009_KEY" in out or sum(FP1 in ln for ln in _lines(proc.stdout)) >= 2


@pytest.mark.fr("FR-008", "FR-001")
def test_fingerprint_env_strips_whitespace(tmp_path):
    inst = _instance(tmp_path)
    _populate(inst, [])
    proc = _run(inst, "--fingerprint-env", "H009_KEY",
                extra_env={"H009_KEY": f"  {KEY_AWORK}\n"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert FP2 in proc.stdout
    assert leaks(proc.stdout + proc.stderr, KEY_AWORK) == []


@pytest.mark.fr("FR-008")
def test_fingerprint_env_unmatched_key_still_listed_rows(tmp_path):
    inst, _ = _populated(tmp_path)
    other = "sk-or-test-DDDDNOTINSTOREKEYMNBVCXZLKJHGF24680"
    proc = _run(inst, "--fingerprint-env", "H009_KEY", extra_env={"H009_KEY": other})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert ref_fp(other) in proc.stdout
    for fp in (FP1, FP2, FP3):
        assert fp in proc.stdout
    assert leaks(proc.stdout + proc.stderr, other) == []


@pytest.mark.fr("FR-008", "FR-006")
def test_cli_never_prints_any_env_key(tmp_path):
    inst, _ = _populated(tmp_path)
    proc = _run(inst, extra_env={"OPENROUTER_API_KEY": KEY_THIRD})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert leaks(proc.stdout + proc.stderr, KEY_THIRD) == []


@pytest.mark.fr("FR-008")
def test_workspace_selects_the_instance(tmp_path):
    """Two instances: the rows shown are the selected instance's."""
    inst_a, _ = _populated(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    inst_b = _instance(other)
    _populate(inst_b, [record(key_fp=ref_fp("sk-or-test-EEEEINSTANCEBKEYQAZWSXEDC11"))])
    out_b = _run(inst_b).stdout
    assert ref_fp("sk-or-test-EEEEINSTANCEBKEYQAZWSXEDC11") in out_b
    assert FP1 not in out_b
    out_a = _run(inst_a).stdout
    assert FP1 in out_a
    assert ref_fp("sk-or-test-EEEEINSTANCEBKEYQAZWSXEDC11") not in out_a
