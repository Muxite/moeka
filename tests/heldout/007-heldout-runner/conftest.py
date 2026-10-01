"""Shared harness for the 007-heldout-runner held-out tests.

Bootstrap rule: this suite is run BY HAND (RUN.md), never through the runner under test.

Safety (spec Test Harness Contract): every test uses its own temp HOME, HELDOUT_ROOT and
TMPDIR, a temp canary git repository and a temp canary suite, and `--profile generic` unless
the test fakes `uv`. The real HOME, the real held-out root and the live checkout are never
touched; processes left behind by a test are SIGKILLed on teardown.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict

import pytest

import _h7

_FR_OF: dict[str, tuple[str, ...]] = {}
_RESULTS: dict[str, str] = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "fr(*ids): requirement ids proved by the test")
    config.addinivalue_line("markers", "slow: takes more than a few seconds")


def pytest_collection_modifyitems(config, items):
    for item in items:
        ids: list[str] = []
        for mark in item.iter_markers("fr"):
            ids.extend(mark.args)
        _FR_OF[item.nodeid] = tuple(dict.fromkeys(ids)) or ("UNMAPPED",)


def pytest_runtest_logreport(report):
    prev = _RESULTS.get(report.nodeid)
    if report.failed:
        _RESULTS[report.nodeid] = "failed" if report.when == "call" else "error"
    elif report.skipped and prev is None:
        _RESULTS[report.nodeid] = "skipped"
    elif report.when == "call" and prev is None:
        _RESULTS[report.nodeid] = "passed"


def pytest_terminal_summary(terminalreporter):
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for nodeid, ids in _FR_OF.items():
        outcome = _RESULTS.get(nodeid)
        if outcome is None:
            continue
        for i in ids:
            stats[i]["total"] += 1
            stats[i][outcome] += 1
    tr = terminalreporter
    tr.section("held-out requirement summary (ids and counts only)")
    for i in sorted(stats):
        s = stats[i]
        tr.write_line(
            f"{i:<8} total={s['total']:>3} passed={s['passed']:>3} failed={s['failed']:>3} "
            f"error={s['error']:>3} skipped={s['skipped']:>3}"
        )
    path = os.environ.get("HELDOUT_REPORT")
    if path:
        with open(path, "w") as fh:
            json.dump({k: dict(v) for k, v in stats.items()}, fh, indent=1, sort_keys=True)


@pytest.fixture(scope="session")
def runner_file():
    if not _h7.RUNNER.is_file():
        pytest.fail("scripts/heldout_run.py is missing from the tree under test", pytrace=False)
    return _h7.RUNNER


@pytest.fixture
def h(runner_file):
    hx = _h7.H()
    try:
        yield hx
    finally:
        hx.close()


@pytest.fixture
def tree(h):
    return h.make_tree()


@pytest.fixture(scope="session")
def mod(runner_file):
    """The module `heldout_run`, loaded from its file path."""
    spec = importlib.util.spec_from_file_location("heldout_run", runner_file)
    m = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("heldout_run")
    sys.modules["heldout_run"] = m
    try:
        spec.loader.exec_module(m)
    except BaseException:
        if saved is None:
            sys.modules.pop("heldout_run", None)
        pytest.fail("module heldout_run cannot be loaded from its file path", pytrace=False)
    return m
