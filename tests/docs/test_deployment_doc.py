"""Spec 005 FR-059, FR-032, FR-062: the deployment and multi-instance pages."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO_ROOT / "docs" / "deployment.md"
MULTI = REPO_ROOT / "docs" / "multiple-instances.md"

STALE = (
    "docker-compose.yml", "docker-compose.bwrap.yml", "/home/nanobot/.nanobot",
    "nanobot-gateway", "nanobot-cli", "NANOBOT_CHANNELS", "NANOBOT_EXTRAS",
)


@pytest.mark.parametrize("name", STALE)
def test_no_stale_docker_names(name: str) -> None:
    assert name not in DEPLOYMENT.read_text()


def test_referenced_repo_files_exist() -> None:
    text = DEPLOYMENT.read_text()
    candidates = set(re.findall(r"`((?:scripts|templates|bin)/[A-Za-z0-9_@./-]+)`", text))
    candidates |= {"Dockerfile", "compose.yaml"} & set(re.findall(r"`([A-Za-z.]+)`", text))
    assert {"Dockerfile", "compose.yaml", "scripts/container-entrypoint.sh"} <= candidates
    missing = [c for c in candidates if not (REPO_ROOT / c).exists()]
    assert missing == []
    for link in re.findall(r"\]\((?:\./)?([a-z-]+\.md)", text):
        assert (DEPLOYMENT.parent / link).exists(), link


def test_deployment_covers_the_container_rules() -> None:
    text = DEPLOYMENT.read_text()
    for needle in ("docker build", "docker compose -p a", "docker compose -p b", "0.0.0.0",
                   "tokenIssueSecret", "MOEKA_OLLAMA_API_BASE", "CAP_SYS_ADMIN",
                   "user namespaces", "bwrap"):
        assert needle in text, needle


def test_multiple_instances_rules() -> None:
    text = MULTI.read_text()
    for needle in ("current config path", "Kernel-native hosts", "One Kernel per state dir",
                   "share a data dir", "memory_key", "file tools only", "exec",
                   "separate UIDs", "bwrap", "containers"):
        assert needle in text, needle


def test_floor_docstring_states_the_limit() -> None:
    from nanobot.security import protected_paths

    doc = protected_paths.__doc__ or ""
    assert "file tools only" in doc and "exec" in doc and "separate" in doc
