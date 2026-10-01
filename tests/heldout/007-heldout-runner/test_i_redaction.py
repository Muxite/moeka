"""Group I: redaction of the feedback channel across every outcome and refusal
(SC-002, FR-024, FR-025, FR-026, US2).

The canary suite puts a unique token into every test name, parametrise id, assertion
message, literal, fixture name and value, marker free text, printed line (stdout, stderr,
raw fds, warnings, logging, terminal summary hook), file name and class name. The temp
base directory name carries a token too, so any leaked path is also a leaked token. The
feedback channel (stdout, stderr, the feedback JSON file) must contain none of them and no
absolute path.
"""

from __future__ import annotations

import os
import shutil

import pytest

import _h7
from _h7 import assert_refused


def canary_suite(h, mode: str) -> dict[str, str]:
    t = {k: h.token() for k in (
        "file", "func", "cls", "pid", "pval", "msg", "lit", "fxname", "fxval", "free",
        "print", "stderr", "fd", "warn", "log", "conf", "hook", "mod", "skip", "xf", "setup")}
    hang = ""
    if mode == "timeout":
        hang = "    _time.sleep(3600)\n"
    test_src = f'''
import logging
import warnings


@pytest.fixture
def fx_{t["fxname"]}():
    return "{t["fxval"]}"


@pytest.fixture
def broken_{t["setup"]}():
    raise RuntimeError("{t["setup"]} " + __file__)


@pytest.mark.fr("FR-001", "free text {t["free"]}")
@pytest.mark.parametrize("v", ["{t["pval"]}"], ids=["{t["pid"]}"])
def test_{t["func"]}(v, fx_{t["fxname"]}):
    _mark_run()
    print("{t["print"]}", __file__, _os.getcwd())
    _sys.stderr.write("{t["stderr"]} " + __file__ + "\\n")
    _os.write(1, b"{t["fd"]}\\n")
    _os.write(2, b"{t["fd"]}\\n")
    warnings.warn("{t["warn"]}")
    logging.getLogger("x").error("{t["log"]}")
{hang}    literal = "{t["lit"]}"
    if {mode in ("fail", "error")!r}:
        assert literal == v, "{t["msg"]} " + fx_{t["fxname"]} + " " + __file__


@pytest.mark.fr("FR-002")
class Test{t["cls"]}:
    def test_m_{t["func"]}(self):
        assert "{t["lit"]}"

    @pytest.mark.skip(reason="{t["skip"]}")
    def test_skip_{t["func"]}(self):
        pass

    @pytest.mark.xfail(reason="{t["xf"]}")
    def test_xf_{t["func"]}(self):
        assert False, "{t["xf"]}"


@pytest.mark.fr("FR-003")
def test_e_{t["func"]}({"broken_" + t["setup"] if mode == "error" else "fx_" + t["fxname"]}):
    pass
'''
    files = {
        f"test_{t['file']}.py": test_src,
        "conftest.py": (
            f'print("{t["conf"]}")\n'
            "def pytest_terminal_summary(terminalreporter):\n"
            f'    terminalreporter.write_line("{t["hook"]} " + __file__)\n'
        ),
        f"helper_{t['mod']}.py": f'VALUE = "{t["mod"]}"\n',
    }
    if mode == "collection_error":
        files[f"test_broken_{t['mod']}.py"] = (
            f'raise ImportError("{t["mod"]} " + __file__)\n')
    return files


def _check(h, res, *files):
    leaks = h.channel(res, *files)
    assert not leaks, f"feedback channel leaks: {sorted(set(leaks))[:5]}"


OUTCOMES = {
    "pass": ((), 0, "passed"),
    "fail": ((), 1, "failed"),
    "error": ((), 1, "failed"),
    "collection_error": ((), 3, "collection_error"),
    "timeout": (("--timeout", "3"), 3, "timeout"),
}


@pytest.mark.fr("SC-002", "FR-024")
@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("mode", sorted(OUTCOMES))
def test_no_leak_per_outcome(h, tree, mode, fmt):
    extra, code, status = OUTCOMES[mode]
    h.write_suite(canary_suite(h, mode))
    ff = h.base / "feedback.json"
    args = list(extra) + ["--feedback-file", ff] + (["--json"] if fmt == "json" else [])
    res = h.run_std(*args)
    assert res.code == code, f"exit {res.code}"
    if fmt == "json":
        assert res.json()["status"] == status
    else:
        assert res.lines()[0] == f"heldout-run: 001-demo round 1/4: {status}"
    assert res.err == ""
    _check(h, res, ff, h.round_dir(1) / "feedback.json")
    assert ff.exists()


@pytest.mark.fr("SC-002", "FR-024", "FR-028")
def test_private_report_keeps_the_details(h, tree):
    files = canary_suite(h, "fail")
    h.write_suite(files)
    res = h.run_std()
    assert res.code == 1
    private = "".join(p.read_text(errors="replace") for p in h.round_dir(1).iterdir()
                      if p.is_file())
    hits = [t for t in h.tokens if t in private]
    assert len(hits) >= 5, "private report does not hold pytest output and failures"


@pytest.mark.fr("SC-002", "FR-024", "FR-014")
def test_no_leak_infra_error_sync(h, tree):
    h.fake_uv(noise=h.token())
    h.env["FAKE_UV_SYNC"] = "fail"
    h.write_suite(canary_suite(h, "fail"))
    ff = h.base / "feedback.json"
    res = h.run_std("--feedback-file", ff, profile="moeka")
    assert_refused(res, "E_SYNC", 3)
    _check(h, res, ff)


@pytest.mark.slow
@pytest.mark.fr("SC-002", "FR-016")
def test_no_leak_busy(h, tree):
    files = canary_suite(h, "pass")
    files["test_block.py"] = '''
@pytest.mark.fr("FR-001")
def test_block():
    _obs("started", {})
    _wait_release("release")
'''
    h.write_suite(files, prelude=True)
    first = h.spawn(*h.std())
    assert _h7.wait_until(lambda: h.obs("started") is not None, 60)
    ff = h.base / "feedback.json"
    res = h.run_std("--feedback-file", ff)
    (h.out / "release").write_text("go")
    first.communicate(timeout=120)
    assert_refused(res, "E_BUSY", 3)
    _check(h, res, ff)


def _setup_refusal(h, case):
    """Returns (args, runner override, expected code, expected exit)."""
    tok = h.token()
    files = canary_suite(h, "fail")
    tree = h.make_tree()
    h.write_suite(files)
    if case == "usage_flag":
        return ["run", f"--bogus-{tok}", tok], None, "E_USAGE", 2
    if case == "usage_value":
        return h.std("--timeout", tok), None, "E_USAGE", 2
    if case == "feature":
        return h.std(feature=f"001-{tok}X"), None, "E_FEATURE", 2
    if case == "repo_name":
        return h.std(repo_name=f"{tok}/x"), None, "E_FEATURE", 2
    if case == "suite_missing":
        return h.std(suite=h.base / f"missing-{tok}"), None, "E_SUITE_MISSING", 2
    if case == "suite_in_tree":
        s = h.write_suite(files, dest=tree / f"hidden-{tok}")
        return h.std(suite=s), None, "E_SUITE_IN_TREE", 2
    if case == "suite_perms":
        os.chmod(h.suite, 0o755)
        return h.std(), None, "E_SUITE_PERMS", 2
    if case == "report_in_tree":
        return h.std("--report-dir", tree / f"rep-{tok}"), None, "E_REPORT_IN_TREE", 2
    if case == "runner_in_tree":
        inside = tree / "scripts" / "heldout_run.py"
        inside.parent.mkdir()
        shutil.copy2(_h7.RUNNER, inside)
        return h.std(), inside, "E_RUNNER_IN_TREE", 2
    if case == "map":
        mp = _h7.write(h.base / f"map-{tok}.json", '{"' + tok + '": ')
        return h.std("--map", mp), None, "E_MAP", 2
    if case == "git":
        return ["run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", str(tree), "--ref", f"ref-{tok}", "--suite", str(h.suite)], \
            None, "E_GIT", 2
    if case == "round_cap":
        h.run_std("--cap", "1")
        return h.std("--cap", "1"), None, "E_ROUND_CAP", 4
    raise AssertionError(case)


REFUSALS = {
    "usage_flag": "FR-025", "usage_value": "FR-025", "feature": "FR-023",
    "repo_name": "FR-023", "suite_missing": "FR-031", "suite_in_tree": "FR-029",
    "suite_perms": "FR-031", "report_in_tree": "FR-029", "runner_in_tree": "FR-005",
    "map": "FR-018", "git": "FR-006", "round_cap": "FR-034",
}


@pytest.mark.fr("SC-002", "FR-025", "FR-026")
@pytest.mark.parametrize("case", [pytest.param(c, marks=pytest.mark.fr(f)) for c, f in
                                  sorted(REFUSALS.items())])
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_no_leak_refusal(h, case, fmt):
    args, runner, code, exit_code = _setup_refusal(h, case)
    runs_before = h.runs()
    if args and args[0] == "run" and fmt == "json":
        args = args + ["--json"]
    ff = h.base / "feedback-refusal.json"
    if args and args[0] == "run" and case not in ("usage_flag",):
        args = args + ["--feedback-file", str(ff)]
    res = h.run(*args, runner=runner)
    assert_refused(res, code, exit_code)
    _check(h, res, ff)
    assert h.runs() == runs_before, "tests ran during a refusal"
