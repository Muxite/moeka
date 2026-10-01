"""Spec 005 FR-056..FR-058: static checks of the image, entrypoint and compose file."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "compose.yaml"
ENTRYPOINT = REPO_ROOT / "scripts" / "container-entrypoint.sh"


def _instructions() -> list[str]:
    text = DOCKERFILE.read_text().replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_dockerfile_user_volume_workspace_and_entrypoint() -> None:
    lines = _instructions()
    users = [ln for ln in lines if ln.startswith("USER ")]
    assert users and users[-1] == "USER 1000:1000"
    assert any(ln.startswith("VOLUME") and "/data" in ln for ln in lines)
    assert any(ln.startswith("ENV") and "MOEKA_WORKSPACE=/data/ws" in ln for ln in lines)
    assert any("scripts/container-entrypoint.sh" in ln for ln in lines if ln.startswith("COPY"))
    entry = [ln for ln in lines if ln.startswith("ENTRYPOINT")]
    cmd = [ln for ln in lines if ln.startswith("CMD")]
    assert entry and "moeka-entrypoint" in entry[-1]
    assert cmd and "gateway" in cmd[-1] and "/data/ws/config.json" in cmd[-1]


def test_dockerfile_has_no_secret() -> None:
    text = DOCKERFILE.read_text()
    assert not re.search(r"(?i)(api_?key|token|secret|password)\s*=", text)
    assert "keys.env" not in text.replace("# ", "")


def test_entrypoint_contract() -> None:
    text = ENTRYPOINT.read_text()
    assert "MOEKA_TOKEN_ISSUE_SECRET" in text and "exit 2" in text
    assert "/data" in text and "UID" in text
    assert "http://host.docker.internal:11434/v1" in text
    assert "templates" in text and "config.json" in text
    secret_check = text.index("MOEKA_TOKEN_ISSUE_SECRET:-")
    assert secret_check < text.rindex("exec nanobot")


def test_compose_contract() -> None:
    data = yaml.safe_load(COMPOSE.read_text())
    [(name, svc)] = data["services"].items()
    assert svc["user"] == "1000:1000"
    assert "moeka-data:/data" in svc["volumes"] and "moeka-data" in data["volumes"]
    ports = svc["ports"]
    assert "127.0.0.1:${MOEKA_GATEWAY_PORT:-18790}:18790" in ports
    assert "127.0.0.1:${MOEKA_WS_PORT:-8765}:8765" in ports
    assert all(p.startswith("127.0.0.1:") for p in ports)
    env = svc["environment"]
    assert env["MOEKA_TOKEN_ISSUE_SECRET"].startswith("${MOEKA_TOKEN_ISSUE_SECRET:?")
    assert "MOEKA_OLLAMA_API_BASE" in env
    assert "host.docker.internal:host-gateway" in svc["extra_hosts"]
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "/health" in " ".join(svc["healthcheck"]["test"])
