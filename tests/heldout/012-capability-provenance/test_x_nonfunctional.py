"""Non-functional: explicit ``cryptography`` dependency (NFR-001), verification speed on a
100-file bundle (NFR-002), the kernel import boundary (NFR-003)."""

from __future__ import annotations

import ast
import os
import re
import statistics
import subprocess
import sys
import time
import tomllib

import pytest
from _h012 import Signer, certify, prov, repo_root, write_skill

REPO = repo_root()

HOST_PACKAGES = (
    "nanobot.channels", "nanobot.web", "nanobot.webui", "nanobot.gateway", "nanobot.pairing",
    "nanobot.audio", "nanobot.cron", "nanobot.triggers", "nanobot.apps", "nanobot.cli",
    "nanobot.optional_features",
)


# -- NFR-001 --------------------------------------------------------------------------------------


@pytest.mark.fr("NFR-001")
def test_cryptography_is_an_explicit_dependency():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    names = [re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0].strip().lower() for d in deps]
    assert "cryptography" in names


@pytest.mark.fr("NFR-001")
def test_cryptography_is_locked():
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "cryptography"' in lock


# -- NFR-002 ----------------------------------------------------------------------------------------


@pytest.mark.fr("NFR-002")
def test_verify_100_file_bundle_under_50ms(tmp_path):
    alpha = Signer("alpha")
    files = {f"data/part-{i // 10}/file-{i:03d}.txt": f"line {i}\n" * 40 for i in range(99)}
    b = write_skill(tmp_path / "ws", "h012-big", extra_files=files)
    assert sum(1 for p in b.rglob("*") if p.is_file()) == 100
    certify(b, alpha)
    trust = prov().TrustStore.from_keys([alpha.pub])
    assert prov().verify_bundle(b, trust).ok  # warm-up and correctness
    samples = []
    for _ in range(9):
        start = time.perf_counter()
        v = prov().verify_bundle(b, trust)
        samples.append(time.perf_counter() - start)
        assert v.ok
    median = statistics.median(samples)
    assert median < 0.050, f"median {median * 1000:.1f} ms"


# -- NFR-003 ----------------------------------------------------------------------------------------


@pytest.mark.fr("NFR-003")
def test_new_modules_exist():
    pkg = REPO / "nanobot" / "capabilities"
    assert (pkg / "provenance.py").is_file() or (pkg / "provenance").is_dir()
    assert (pkg / "script_tool.py").is_file() or (pkg / "script_tool").is_dir()


@pytest.mark.fr("NFR-003")
def test_capabilities_package_imports_no_host_package():
    pkg = REPO / "nanobot" / "capabilities"
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


@pytest.mark.fr("NFR-003")
def test_importing_new_modules_leaks_no_host_module():
    probe = (
        "import sys\n"
        "import nanobot.capabilities.provenance, nanobot.capabilities.script_tool\n"
        f"host = {HOST_PACKAGES!r}\n"
        "print(sorted(m for m in sys.modules if any(m == h or m.startswith(h + '.') "
        "for h in host)))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().splitlines()[-1] == "[]"


@pytest.mark.fr("NFR-003")
@pytest.mark.timeout(600)
def test_repo_import_boundary_guard_passes():
    guard = REPO / "tests" / "core" / "test_import_boundary.py"
    if not guard.is_file():
        pytest.skip("tests/core/test_import_boundary.py not in the copy")
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(REPO),
         str(guard)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=560,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
