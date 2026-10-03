"""Shared harness for the 009-luna-key-attribution held-out tests.

Safety (spec Test Harness Contract): offline (providers never reach the network: ``chat`` is
overridden, and any stray HTTP goes to a dead local proxy), HOME is a fresh temporary
directory for the session and per test, every usage store lives in a per-test temp dir, no
real API key is ever read (all key variables are removed; keys are fake literals), and the
suite refuses the live checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# -- session-wide isolation (before anything imports nanobot) ----------------------------

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="h009s"))
for _v in (
    "MOEKA_WORKSPACE", "MOEKA_CONFIG", "MOEKA_STATE", "NANOBOT_HOME", "MOEKA_REPO_ENV",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
):
    os.environ.pop(_v, None)
os.environ["HOME"] = str(_SESSION_HOME)
DEAD_PROXY = "http://127.0.0.1:9"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ[_k] = DEAD_PROXY
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

_spec = importlib.util.find_spec("nanobot")
if _spec is None or _spec.origin is None:
    raise RuntimeError("nanobot is not importable; set PYTHONPATH=<implementer tree copy>")
REPO = Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(_spec.origin).resolve().parents[1])
if REPO.resolve() == Path("/home/muk/projects/moeka").resolve():
    raise RuntimeError("refusing to run held-out tests against the live checkout")

try:  # quiet by default; FR-006 tests add their own capturing sink
    from loguru import logger as _logger

    _logger.remove()
except Exception:  # noqa: BLE001
    pass


# -- requirement-id reporting (ids and counts only) ----------------------------------------

_FR_OF: dict[str, tuple[str, ...]] = {}
_RESULTS: dict[str, str] = {}


def pytest_collection_modifyitems(config, items):
    for item in items:
        ids: list[str] = []
        for mark in item.iter_markers("fr"):
            ids.extend(mark.args)
        _FR_OF[item.nodeid] = tuple(dict.fromkeys(ids)) or ("UNMAPPED",)


def pytest_runtest_logreport(report):
    prev = _RESULTS.get(report.nodeid)
    if report.when == "call" or (report.when in ("setup", "teardown") and report.outcome != "passed"):
        outcome = report.outcome
        if report.when != "call" and report.outcome == "failed":
            outcome = "error"
        if prev in ("failed", "error"):
            return
        _RESULTS[report.nodeid] = outcome


def _fr_summary() -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for nodeid, outcome in _RESULTS.items():
        for fr in _FR_OF.get(nodeid, ("UNMAPPED",)):
            summary[fr][outcome] += 1
            summary[fr]["total"] += 1
    return {k: dict(v) for k, v in sorted(summary.items())}


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    summary = _fr_summary()
    tr = terminalreporter
    tr.section("held-out requirement summary (ids and counts only)")
    failing = 0
    for fr, counts in summary.items():
        bad = counts.get("failed", 0) + counts.get("error", 0)
        failing += bool(bad)
        tr.write_line(
            f"{fr:8s} total={counts.get('total', 0):3d} passed={counts.get('passed', 0):3d} "
            f"failed={counts.get('failed', 0):3d} error={counts.get('error', 0):3d} "
            f"skipped={counts.get('skipped', 0):3d}"
        )
    tr.write_line(f"requirement ids with >=1 failing test: {failing}")
    out = os.environ.get("HELDOUT_REPORT")
    if out:
        Path(out).write_text(json.dumps(summary, indent=2) + "\n")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


# -- per-test isolation ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MOEKA_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def store(tmp_path):
    """A fresh ``LLMUsageStore`` under a per-test ``data_dir`` (closed afterwards)."""
    from nanobot.llm_usage.store import LLMUsageStore

    from _h009 import DB_NAME

    s = LLMUsageStore(tmp_path / "data" / DB_NAME)
    yield s
    try:
        s.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def log_lines():
    """Every loguru message (all levels, with exception text) and stdlib log record."""
    import logging

    from loguru import logger

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level=0, backtrace=False,
                         diagnose=False, format="{level} {name} {message}\n{exception}")

    class _H(logging.Handler):
        def emit(self, record):  # noqa: D401
            try:
                lines.append(self.format(record))
            except Exception:  # noqa: BLE001
                lines.append(str(record.msg))

    handler = _H(level=0)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(0)
    yield lines
    root.removeHandler(handler)
    root.setLevel(old_level)
    logger.remove(sink_id)
