"""Non-functional: speed (NFR-001), the labelled benchmark (NFR-002), no new runtime dependency
(NFR-003), the import boundary (NFR-004), SC-004."""

from __future__ import annotations

import ast
import json
import os
import random
import subprocess
import sys
import time
import tomllib

import pytest
from _h011 import (
    EFFECTS,
    INPUT_TYPES,
    TYPES,
    close_index,
    const_ranker,
    entry,
    open_index,
    repo_root,
    run_select,
)

REPO = repo_root()

# [project].dependencies on consolidate/new-main before this spec.
BASE_DEPENDENCIES = {
    "typer>=0.20.0,<1.0.0", "anthropic>=0.100.0,<1.0.0", "pydantic>=2.12.0,<3.0.0",
    "pydantic-settings>=2.12.0,<3.0.0", "websockets>=15.0,<17.0", "httpx[socks]>=0.28.0,<1.0.0",
    "ddgs>=9.5.5,<10.0.0", "oauth-cli-kit>=0.1.6,<1.0.0", "loguru>=0.7.3,<1.0.0",
    "readability-lxml>=0.8.4,<1.0.0", "lxml-html-clean>=0.4.0,<1.0.0", "rich>=14.0.0,<15.0.0",
    "qrcode[pil]>=8.0", "croniter>=6.0.0,<7.0.0", "prompt-toolkit>=3.0.50,<4.0.0",
    "setproctitle>=1.3.7,<2.0.0; sys_platform != 'win32'", "questionary>=2.0.0,<3.0.0",
    "mcp>=1.26.0,<2.0.0", "json-repair>=0.57.0,<1.0.0", "chardet>=3.0.2,<6.0.0",
    "openai>=2.8.0", "tiktoken>=0.12.0,<1.0.0", "jinja2>=3.1.0,<4.0.0",
    "dulwich>=0.22.0,<1.0.0", "rapidfuzz>=3.14.6,<4.0.0", "pyyaml>=6.0,<7.0.0",
    "filelock>=3.25.2", "watchfiles>=1.1.1,<2.0.0", "packaging>=24.0", "tzdata>=2025.2",
    "tzlocal>=5.3.1,<6.0.0", "defusedxml>=0.7.1,<1.0.0", "pypdf>=5.0.0,<6.0.0",
    "python-docx>=1.1.0,<2.0.0", "openpyxl>=3.1.0,<4.0.0", "python-pptx>=1.0.0,<2.0.0",
}

# Allow-list keys on consolidate/new-main before this spec.
BASE_AMBIENT = {"nanobot/cli/", "nanobot/config/", "nanobot/kernel/legacy.py",
                "nanobot/utils/restart.py"}
BASE_HOST_AMBIENT = {
    "nanobot/agent/tools/message.py", "nanobot/api/server.py", "nanobot/apps/", "nanobot/audio/",
    "nanobot/channels/", "nanobot/gateway/", "nanobot/optional_features.py", "nanobot/pairing/",
    "nanobot/webui/",
}
BASE_EXEMPTIONS = {"nanobot/utils/path.py"}
HOST_PACKAGES = (
    "nanobot.channels", "nanobot.web", "nanobot.webui", "nanobot.gateway", "nanobot.pairing",
    "nanobot.audio", "nanobot.cron", "nanobot.triggers", "nanobot.apps", "nanobot.cli",
    "nanobot.optional_features",
)


# -- NFR-001 / SC-004 ---------------------------------------------------------------------------


def _library(n, seed=1000):
    rng = random.Random(seed)
    lib = []
    for i in range(n):
        io = {"inputs": {"all": rng.sample(INPUT_TYPES, rng.randint(0, 2)),
                         "any": []},
              "outputs": rng.sample(TYPES, rng.randint(1, 2)),
              "effects": rng.sample(EFFECTS, rng.randint(1, 2))}
        io["inputs"]["any"] = [t for t in rng.sample(INPUT_TYPES, rng.randint(0, 2))
                               if t not in io["inputs"]["all"]]
        lib.append(entry(rng.choice(["skill", "tool"]), f"cap-{i:04d}", f"capability {i}", io))
    return lib


@pytest.mark.bench
@pytest.mark.fr("NFR-001", "SC-004")
@pytest.mark.timeout(600)
def test_filter_p99_under_5ms_on_1000_capabilities(tmp_path):
    idx = open_index(tmp_path / "bench.db")
    try:
        idx.sync(_library(1000))
        rng = random.Random(5)
        task_sets = [set(rng.sample([t for t in INPUT_TYPES if t != "any"], 8)) for _ in range(50)]
        ambient = ("text/query", "workspace")  # 8 extracted + 2 ambient = 10 task types
        for types in task_sets[:5]:
            run_select(idx, types, "text/markdown", ambient=ambient, ranker=const_ranker)
        samples = []
        for i in range(400):
            types = task_sets[i % len(task_sets)]
            start = time.perf_counter()
            run_select(idx, types, rng.choice([None, "text/markdown", "data/csv"]),
                       ambient=ambient, ranker=const_ranker)
            samples.append(time.perf_counter() - start)
        samples.sort()
        p99 = samples[int(len(samples) * 0.99) - 1]
        assert p99 < 0.005, f"p99 {p99 * 1000:.2f} ms"
    finally:
        close_index(idx)


# -- NFR-002 / SC-004 ---------------------------------------------------------------------------


@pytest.mark.fr("NFR-002", "SC-004")
def test_labelled_benchmark_cases_exist():
    path = REPO / "tests" / "capabilities" / "bench" / "cases.jsonl"
    assert path.is_file(), "tests/capabilities/bench/cases.jsonl missing"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(cases) >= 150
    expected_names = set()
    for case in cases:
        assert isinstance(case, dict)
        assert isinstance(case.get("message"), str) and case["message"]
        assert "attachments" in case and isinstance(case["attachments"], list)
        exp = case.get("expected")
        assert isinstance(exp, list) and exp and all(isinstance(n, str) for n in exp)
        expected_names.update(exp)
    assert len({c["message"] for c in cases}) >= 150, "duplicate messages"
    assert len(expected_names) >= 20


@pytest.mark.fr("NFR-002", "SC-004")
@pytest.mark.timeout(560)
def test_repo_benchmark_test_passes():
    """The repo's own quality benchmark (a test under tests/capabilities that reads
    cases.jsonl and asserts recall@8 / token reduction) runs green from the copy."""
    root = REPO / "tests" / "capabilities"
    tests = sorted(p for p in root.rglob("test_*.py")
                   if "cases.jsonl" in p.read_text(encoding="utf-8", errors="replace"))
    if not tests:
        pytest.fail("no test under tests/capabilities reads cases.jsonl")
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "--rootdir", str(REPO),
         "-m", "", *map(str, tests)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=500,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    summary = [ln for ln in proc.stdout.splitlines() if " passed" in ln]
    # Head amendment (round 1): NFR-001's timing benchmark may skip itself outside `-m bench`
    # (spec: "excluded from the default run"); any other skip still fails this test.
    assert summary, proc.stdout[-2000:]
    if "skipped" in summary[-1]:
        reasons = [ln for ln in proc.stdout.splitlines() if ln.startswith("SKIPPED")]
        assert reasons and all("bench" in ln for ln in reasons), proc.stdout[-2000:]


# -- NFR-003 ------------------------------------------------------------------------------------


@pytest.mark.fr("NFR-003")
def test_no_new_runtime_dependency():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = set(data["project"]["dependencies"])
    # Head amendment (2026-10-03): spec 012 NFR-001 later makes `cryptography` (already a transitive
    # dependency) explicit; that one later addition is allowed. 011 itself adds nothing.
    later = {d for d in deps if d.split(">")[0].split("=")[0].split("<")[0].strip() == "cryptography"}
    assert deps - later <= BASE_DEPENDENCIES, sorted(deps - later - BASE_DEPENDENCIES)


@pytest.mark.fr("NFR-003", "NFR-004")
def test_capabilities_import_without_vec_extra():
    probe = (
        "import sys\n"
        "import nanobot.capabilities.types, nanobot.capabilities.manifest\n"
        "import nanobot.capabilities.index, nanobot.capabilities.task_types\n"
        "import nanobot.capabilities.select\n"
        "print('sqlite_vec' in sys.modules)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().splitlines()[-1] == "False"


# -- NFR-004 ------------------------------------------------------------------------------------


@pytest.mark.fr("NFR-004")
def test_capabilities_package_imports_no_host_package():
    pkg = REPO / "nanobot" / "capabilities"
    assert pkg.is_dir()
    offenders = []
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in HOST_PACKAGES):
                    offenders.append((path.name, name))
                if name == "nanobot.config.loader" or name.startswith("nanobot.config.loader."):
                    offenders.append((path.name, name))
    assert offenders == []


@pytest.mark.fr("NFR-004")
def test_capabilities_import_leaks_no_host_module():
    probe = (
        "import sys\n"
        "import nanobot.capabilities.types, nanobot.capabilities.manifest\n"
        "import nanobot.capabilities.index, nanobot.capabilities.task_types\n"
        "import nanobot.capabilities.select\n"
        f"forbidden = {HOST_PACKAGES!r}\n"
        "print(','.join(sorted(m for m in sys.modules\n"
        "                      if any(m == p or m.startswith(p + '.') for p in forbidden))))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    leaked = [m for m in last.split(",") if m]
    assert leaked == []


def _keys(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id in names:
            value = node.value
            if isinstance(value, ast.Dict):
                out[target.id] = {ast.literal_eval(k) for k in value.keys}
            else:
                out[target.id] = set(ast.literal_eval(value))
    return out


@pytest.mark.fr("NFR-004")
def test_no_new_allowlist_entries():
    ambient = REPO / "tests" / "kernel" / "test_no_ambient_reads.py"
    boundary = REPO / "tests" / "core" / "test_import_boundary.py"
    if not ambient.exists() or not boundary.exists():
        pytest.skip("repo tests not in the copy")
    got = _keys(ambient, {"AMBIENT_ALLOWLIST", "HOST_AMBIENT_ALLOWLIST", "KNOWN_EXEMPTIONS"})
    assert got["AMBIENT_ALLOWLIST"] <= BASE_AMBIENT
    assert got["HOST_AMBIENT_ALLOWLIST"] <= BASE_HOST_AMBIENT
    assert got["KNOWN_EXEMPTIONS"] <= BASE_EXEMPTIONS
    assert _keys(boundary, {"_FORBIDDEN"})["_FORBIDDEN"] >= set(HOST_PACKAGES)


@pytest.mark.fr("NFR-004")
@pytest.mark.timeout(400)
def test_repo_guards_pass():
    files = [REPO / "tests" / "core" / "test_import_boundary.py",
             REPO / "tests" / "kernel" / "test_no_ambient_reads.py"]
    if not all(f.exists() for f in files):
        pytest.skip("repo tests not in the copy")
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x",
         "--rootdir", str(REPO), *map(str, files)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=380,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
