"""Shared harness for the 012-capability-provenance held-out tests.

Safety (spec Test Harness Contract): offline (FakeProvider only; any stray HTTP goes to a
dead local proxy), HOME is a fresh temporary directory for the session and per test, every
kernel / workspace / trust dir lives in a per-test temp dir, keys are deterministic FAKE
Ed25519 keys made in the tests, and the suite refuses the live checkout.
``nanobot.capabilities.provenance`` / ``script_tool`` are imported lazily inside tests
(``_h012.cap``) so a tree without the feature collects cleanly and fails per requirement id.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent

# -- session-wide isolation (before anything imports nanobot) ----------------------------

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="h012s"))
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

try:  # quiet: the agent loop logs every stage at DEBUG
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
    yield home


@pytest.fixture
def home(_fresh_home):
    return _fresh_home


@pytest.fixture
def sink():
    from moeka.trace import MemoryTraceSink

    return MemoryTraceSink()


@pytest.fixture
def logs():
    """Every loguru record (all levels) emitted during the test: ``[(level_no, level, msg)]``."""
    from loguru import logger

    records: list[tuple[int, str, str]] = []

    def _sink(message: Any) -> None:
        rec = message.record
        records.append((rec["level"].no, rec["level"].name, str(rec["message"])))

    hid = logger.add(_sink, level=0, format="{message}")
    yield records
    try:
        logger.remove(hid)
    except ValueError:
        pass


@pytest.fixture
def warned(logs, caplog, recwarn):
    """``warned()`` -> every warning-level message so far, from loguru (WARNING+), the
    ``warnings`` module and stdlib ``logging`` (WARNING+); any channel counts as "a warning"."""
    import logging

    caplog.set_level(logging.WARNING)

    def collect() -> list[str]:
        out = [msg for no, _lvl, msg in logs if no >= 30]
        out += [str(w.message) for w in recwarn.list]
        out += [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        return out

    return collect


@pytest.fixture
def kmaker(tmp_path, sink):
    """``make(*, work, caps=None, script_enabled=None, builtin=None, trace=None, fake=None)``
    -> ``(kernel, fake)``: a kernel over ``Config(capabilities=caps, tools.script.enabled)``
    with its own state dir, closed at teardown."""
    kernels: list[Any] = []

    def make(*, work: Path, caps: Any = None, script_enabled: bool | None = None,
             builtin: Path | None = None, trace: Any = None, fake: Any = None) -> Any:
        from _h012 import make_kernel

        kernel, fk = make_kernel(
            tmp_path / f"kstate{len(kernels)}", caps=caps, script_enabled=script_enabled,
            builtin=builtin, work=work, trace=trace if trace is not None else sink, fake=fake,
        )
        kernels.append(kernel)
        return kernel, fk

    yield make
    for kernel in kernels:
        try:
            kernel.close()
        except Exception:  # noqa: BLE001
            pass
