"""Cross-cutting: public exports (FR-041) and the repo guards (FR-042)."""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import sys

import pytest
from _h006 import repo_root

REPO = repo_root()

# Allow-list keys on consolidate/new-main (c5340d24) before this spec.
BASE_AMBIENT = {"nanobot/cli/", "nanobot/config/", "nanobot/kernel/legacy.py",
                "nanobot/utils/restart.py"}
BASE_HOST_AMBIENT = {
    "nanobot/agent/tools/message.py", "nanobot/api/server.py", "nanobot/apps/", "nanobot/audio/",
    "nanobot/channels/", "nanobot/gateway/", "nanobot/optional_features.py", "nanobot/pairing/",
    "nanobot/webui/",
}
BASE_EXEMPTIONS = {"nanobot/utils/path.py"}
BASE_FORBIDDEN = {
    "nanobot.channels", "nanobot.web", "nanobot.webui", "nanobot.gateway", "nanobot.pairing",
    "nanobot.audio", "nanobot.cron", "nanobot.triggers", "nanobot.apps", "nanobot.cli",
    "nanobot.optional_features",
}


@pytest.mark.fr("FR-041")
def test_public_exports():
    from moeka import ModelSpec
    from moeka.agents import AgentSpec
    from moeka.errors import VariantError
    from moeka.trace import args_digest
    from moeka.variants import Variant

    assert callable(args_digest) and issubclass(VariantError, ValueError)
    assert "on_unsupported" in {f.name for f in dataclasses.fields(AgentSpec)}
    assert "tool_param_descriptions" in {f.name for f in dataclasses.fields(Variant)}
    assert "unsupported_sampling" in {f.name for f in dataclasses.fields(ModelSpec)}
    import moeka.errors
    import moeka.trace

    for mod, name in ((moeka.trace, "args_digest"), (moeka.errors, "VariantError")):
        if hasattr(mod, "__all__"):
            assert name in mod.__all__, name


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


@pytest.mark.fr("FR-042")
def test_no_new_allowlist_entries():
    ambient = REPO / "tests" / "kernel" / "test_no_ambient_reads.py"
    boundary = REPO / "tests" / "core" / "test_import_boundary.py"
    if not ambient.exists() or not boundary.exists():
        pytest.skip("repo tests not in the copy")
    got = _keys(ambient, {"AMBIENT_ALLOWLIST", "HOST_AMBIENT_ALLOWLIST", "KNOWN_EXEMPTIONS"})
    assert got["AMBIENT_ALLOWLIST"] <= BASE_AMBIENT
    assert got["HOST_AMBIENT_ALLOWLIST"] <= BASE_HOST_AMBIENT
    assert got["KNOWN_EXEMPTIONS"] <= BASE_EXEMPTIONS
    assert _keys(boundary, {"_FORBIDDEN"})["_FORBIDDEN"] >= BASE_FORBIDDEN


@pytest.mark.fr("FR-042")
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
