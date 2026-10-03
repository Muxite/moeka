"""Cross-cutting: no new dependency (NFR-001) and existing usage/ledger/schema tests (NFR-002)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib

import pytest
from _h009 import repo_root

REPO = repo_root()

# [project].dependencies names on consolidate/new-main before this spec (9920dafb).
BASE_DEPS = {
    "typer", "anthropic", "pydantic", "pydantic-settings", "websockets", "httpx", "ddgs",
    "oauth-cli-kit", "loguru", "readability-lxml", "lxml-html-clean", "rich", "qrcode",
    "croniter", "prompt-toolkit", "setproctitle", "questionary", "mcp", "json-repair",
    "chardet", "openai", "tiktoken", "jinja2", "dulwich", "rapidfuzz", "pyyaml", "filelock",
    "watchfiles", "packaging", "tzdata", "tzlocal", "defusedxml", "pypdf", "python-docx",
    "openpyxl", "python-pptx",
}
# Files that already imported a crypto/hash package before this spec (channels only).
BASE_HASH_IMPORTERS = {
    "nanobot/channels/msteams/tests/test_msteams.py", "nanobot/channels/weixin/runtime.py",
}
GUARD_TESTS = [
    "tests/llm_usage",
    "tests/kernel/test_ledger.py",
    "tests/kernel/test_usage_surface.py",
    "tests/schemas/test_schemas.py",
    "tests/providers/test_llm_usage_observer.py",
]


def _name(req: str) -> str:
    return re.split(r"[\s\[<>=!~;]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


@pytest.mark.fr("NFR-001")
def test_no_new_runtime_dependency():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = {_name(d) for d in data["project"]["dependencies"]}
    # Head amendment (2026-10-03): spec 012 NFR-001 later makes `cryptography` explicit (signature
    # verification); 009 itself adds nothing.
    assert deps - {"cryptography"} <= BASE_DEPS, sorted(deps - {"cryptography"} - BASE_DEPS)


@pytest.mark.fr("NFR-001")
def test_no_third_party_hash_library_imported():
    """The fingerprint is hashlib's sha256; no hashing package is pulled in for it."""
    banned = re.compile(r"^\s*(import|from)\s+(xxhash|blake3|mmh3|cryptography|nacl|Crypto)\b",
                        re.M)
    # Head amendment (2026-10-03): spec 012's certificate verification under nanobot/capabilities/
    # legitimately imports `cryptography`; the key fingerprint must still use hashlib only.
    hits = [str(p.relative_to(REPO)) for p in (REPO / "nanobot").rglob("*.py")
            if banned.search(p.read_text(encoding="utf-8", errors="replace"))
            and not p.relative_to(REPO).as_posix().startswith("nanobot/capabilities/")]
    assert set(hits) <= BASE_HASH_IMPORTERS, sorted(set(hits) - BASE_HASH_IMPORTERS)


@pytest.mark.fr("NFR-002")
@pytest.mark.timeout(400)
def test_existing_usage_ledger_schema_tests_pass():
    files = [REPO / f for f in GUARD_TESTS]
    present = [str(f) for f in files if f.exists()]
    if not present:
        pytest.skip("repo tests not in the copy")
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x",
         "--rootdir", str(REPO), *present],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=380,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
