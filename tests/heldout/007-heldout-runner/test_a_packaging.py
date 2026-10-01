"""Group A: packaging and trust (FR-001..005, FR-039)."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _h7
from _h7 import RUNNER, WRAPPER, assert_refused

FORBIDDEN_ROOTS = {"moeka", "nanobot", "awr"}


def _imports(tree: ast.AST):
    """(module-level?, top-level name) for every import in the file."""
    found = []

    def visit(node, in_func):
        for child in ast.iter_child_nodes(node):
            inner = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, ast.Import):
                for a in child.names:
                    found.append((not in_func, a.name.split(".")[0]))
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    found.append((not in_func, child.module.split(".")[0]))
            visit(child, inner)

    visit(tree, False)
    return found


@pytest.mark.fr("FR-001")
def test_module_level_imports_are_stdlib_only(runner_file):
    tree = ast.parse(runner_file.read_text())
    bad = [n for top, n in _imports(tree) if top and n != "__future__"
           and n not in sys.stdlib_module_names]
    assert not bad, "module-level import outside the standard library"


@pytest.mark.fr("FR-001")
def test_no_import_of_moeka_nanobot_awr_or_third_party(runner_file):
    tree = ast.parse(runner_file.read_text())
    names = [n for _top, n in _imports(tree)]
    assert not FORBIDDEN_ROOTS & set(names), "imports moeka, nanobot or awr"
    allowed = set(sys.stdlib_module_names) | {"pytest", "_pytest", "__future__", "heldout_run"}
    assert not [n for n in names if n not in allowed], "imports a third-party package"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            assert not FORBIDDEN_ROOTS & {str(a).split(".")[0] for a in args}


@pytest.mark.fr("FR-001")
def test_importable_as_heldout_run_from_its_directory(h):
    code = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        "import heldout_run\n"
        "new = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "bad = sorted(n for n in new if n not in sys.stdlib_module_names\n"
        "             and not n.startswith('_') and n != 'heldout_run')\n"
        "print(json.dumps({'file': heldout_run.__file__, 'bad': bad,\n"
        "                  'pytest': 'pytest' in sys.modules}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=RUNNER.parent, env=h.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "import heldout_run failed from scripts/"
    import json
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert Path(data["file"]).resolve() == RUNNER.resolve()
    assert data["bad"] == [], "importing the module loads non-stdlib modules"
    assert data["pytest"] is False, "pytest imported at module level"


@pytest.mark.fr("FR-001")
def test_import_has_no_side_effects(h):
    before = _h7.snapshot(h.base)
    r = subprocess.run([sys.executable, "-c", "import heldout_run"], cwd=RUNNER.parent,
                       env=h.env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""
    assert _h7.snapshot(h.base) == before


@pytest.mark.fr("FR-002")
def test_version_line_and_sha256(h):
    res = h.run("--version")
    assert res.code == 0
    assert res.err == ""
    m = re.fullmatch(r"heldout-run (\S+) sha256:([0-9a-f]{64})\n?", res.out)
    assert m, "--version output does not match 'heldout-run <version> sha256:<hex>'"
    assert m.group(2) == hashlib.sha256(RUNNER.read_bytes()).hexdigest()


@pytest.mark.fr("FR-002")
def test_wrapper_is_executable_and_prints_same_version(h):
    assert WRAPPER.is_file(), "scripts/heldout-run is missing"
    assert os.access(WRAPPER, os.X_OK), "scripts/heldout-run is not executable"
    a = h.run("--version")
    b = h.run("--version", wrapper=True)
    assert b.code == 0 and b.err == ""
    assert b.out == a.out


@pytest.mark.fr("FR-002")
def test_wrapper_passes_all_arguments(h, tree):
    h.python3_shim()
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    res = h.run(*h.std(), wrapper=True)
    assert res.code == 1
    assert res.lines()[0] == "heldout-run: 001-demo round 1/4: failed"
    r = h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset",
              "--reason", "tester bug with spaces", wrapper=True)
    assert r.code == 0
    data = h.rounds_json()
    assert data["rounds"] == []
    assert data["resets"][-1]["reason"] == "tester bug with spaces"
    bad = h.run("run", "--no-such-flag", wrapper=True)
    assert_refused(bad, "E_USAGE", 2)


@pytest.mark.fr("FR-004")
def test_run_heldout_sh_unchanged():
    p = _h7.REPO / "scripts" / "run-heldout.sh"
    assert p.is_file(), "scripts/run-heldout.sh is missing"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == _h7.RUN_HELDOUT_SH_SHA256


@pytest.mark.fr("FR-003", "FR-004")
def test_awork_resume_vendored_copy_optional():
    """Runs only with HELDOUT_007_AWORK=<throwaway copy of the awork-resume tree>."""
    aw = os.environ.get("HELDOUT_007_AWORK")
    if not aw:
        pytest.skip("HELDOUT_007_AWORK not set (awork-resume tree not under test)")
    aw = Path(aw)
    for rel in ("scripts/heldout_run.py", "scripts/heldout-run"):
        assert (aw / rel).read_bytes() == (_h7.REPO / rel).read_bytes(), "vendored copy differs"
    assert os.access(aw / "scripts" / "heldout-run", os.X_OK)
    src = (aw / "scripts" / "heldout_run.SOURCE").read_text()
    assert re.fullmatch(r"[0-9a-f]{40}\n?", src), "SOURCE is not one 40-hex line"
    sh = aw / "scripts" / "run-heldout.sh"
    assert hashlib.sha256(sh.read_bytes()).hexdigest() == _h7.AWORK_RUN_HELDOUT_SH_SHA256


@pytest.mark.fr("FR-005")
def test_runner_inside_tree_under_test_is_refused(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    inside = tree / "scripts" / "heldout_run.py"
    inside.parent.mkdir()
    shutil.copy2(RUNNER, inside)
    res = h.run(*h.std(), runner=inside)
    assert_refused(res, "E_RUNNER_IN_TREE", 2)
    assert h.runs() == 0, "tests ran although the runner sits in the tree"
    assert h.leftovers() == []
    assert h.counted() == 0


@pytest.mark.fr("FR-005")
def test_runner_reached_through_symlink_into_tree_is_refused(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    inside = tree / "tools" / "heldout_run.py"
    inside.parent.mkdir()
    shutil.copy2(RUNNER, inside)
    link_dir = _h7.mkd(h.base / "linkdir")
    (link_dir / "heldout_run.py").symlink_to(inside)
    res = h.run(*h.std(), runner=link_dir / "heldout_run.py")
    assert_refused(res, "E_RUNNER_IN_TREE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-039")
def test_stated_limit_in_module_docstring(mod):
    doc = (mod.__doc__ or "").lower()
    assert re.search(r"sandbox|adversarial|hostile", doc), "docstring does not state the limit"
    assert re.search(r"\bnot\b|\bno\b", doc)


@pytest.mark.fr("FR-039")
def test_stated_limit_in_help(h):
    res = h.run("--help")
    assert res.code == 0
    text = res.out.lower()
    assert re.search(r"sandbox|adversarial|hostile", text), "--help does not state the limit"
