"""Shared harness for the 008-heldout-sandbox held-out tests.

Bootstrap rule: run this suite with the HEAD's trusted runner (unsandboxed, generic profile) or
by hand (RUN.md), never with the runner of the tree under test.

Safety (007 Test Harness Contract): every test uses its own temp HOME, HELDOUT_ROOT and TMPDIR,
a temp toy git repository and a temp toy suite, and `--profile generic` (except the slow parity
tests, which use `--profile moeka` on a copy of the tree). The real HOME, the real held-out root,
`~/.nanobot` and the live checkout are never touched. Containers, networks and images a test
creates are removed on teardown; the `heldout-sandbox:*` image is kept as a cache.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict

import pytest

import _h8

_FR_OF: dict[str, tuple[str, ...]] = {}
_RESULTS: dict[str, str] = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "fr(*ids): requirement ids proved by the test")
    config.addinivalue_line("markers", "slow: takes more than a minute")
    config.addinivalue_line("markers", "docker: needs a Docker daemon (runs unsandboxed)")
    config.addinivalue_line("markers", "network: containers need registry (PyPI) access")


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
    if not _h8.RUNNER.is_file():
        pytest.fail("scripts/heldout_run.py is missing from the tree under test", pytrace=False)
    return _h8.RUNNER


@pytest.fixture(scope="session")
def mod(runner_file):
    """The module `heldout_run`, loaded from its file path."""
    spec = importlib.util.spec_from_file_location("heldout_run_008", runner_file)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except BaseException:
        pytest.fail("module heldout_run cannot be loaded from its file path", pytrace=False)
    return m


@pytest.fixture
def h(runner_file):
    hx = _h8.H()
    try:
        yield hx
    finally:
        hx.close()


@pytest.fixture
def tree(h):
    return h.make_tree()


_WARM: dict = {}


@pytest.fixture(scope="session")
def image(mod):
    """The default sandbox image tag; warmed by one sandboxed run (builds it when missing)."""
    if not _h8.docker_available():
        pytest.fail("Docker daemon not reachable; docker-marked tests need it", pytrace=False)
    text = getattr(mod, "SANDBOX_DOCKERFILE", None)
    if not isinstance(text, str) or not text.strip():
        pytest.fail("heldout_run.SANDBOX_DOCKERFILE missing", pytrace=False)
    tag = _h8.image_tag_for(text)
    if "res" not in _WARM:
        _WARM["existed_before"] = _h8.image_id(tag) is not None
        hx = _h8.H()
        try:
            hx.make_tree()
            hx.write_suite({"test_warm.py": _h8.ALL_PASS_SUITE})
            res = hx.run_sb(timeout=3600)
            _WARM["res"] = res
            _WARM["report_exists"] = (res.report_dir / "report.json").is_file()
        finally:
            hx.close()
    if _h8.image_id(tag) is None:
        pytest.fail(f"sandbox image {tag} was not built by a sandboxed run", pytrace=False)
    os.environ["_H8_CLEAN_IMAGE"] = tag
    return tag


@pytest.fixture(scope="session")
def warm():
    return _WARM


@pytest.fixture
def no_new_containers(image):
    """Asserts after the test body that no sandbox container created during it remains."""
    before = set(_h8.sandbox_containers())
    yield
    left = set(_h8.sandbox_containers()) - before
    for c in left:
        _h8.docker("rm", "-f", "-v", c, timeout=60)
    assert not left, f"{len(left)} sandbox container(s) left after the run"
