"""Spec 005 FR-056..FR-058 (SC-001): the image and compose file, against a real docker.

Marked ``docker``: skipped when no docker daemon answers. Images, containers, compose
projects and volumes get unique names and are removed afterwards.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker(*args: str, timeout: float = 600, env: dict[str, str] | None = None,
            check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          env=env, check=check, cwd=str(REPO_ROOT))


@pytest.fixture(scope="module")
def image() -> str:
    tag = os.environ.get("MOEKA_CONTAINER_TEST_IMAGE") or f"moeka-test-005:{uuid.uuid4().hex[:8]}"
    if not os.environ.get("MOEKA_CONTAINER_TEST_IMAGE"):
        built = _docker("build", "-t", tag, ".", timeout=1800)
        assert built.returncode == 0, built.stderr[-4000:]
    yield tag
    if not os.environ.get("MOEKA_CONTAINER_TEST_IMAGE"):
        _docker("image", "rm", "-f", tag)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def test_image_runs_as_uid_1000(image: str) -> None:
    result = _docker("run", "--rm", "--entrypoint", "id", image, "-u")
    assert result.stdout.strip() == "1000"
    inspect = json.loads(_docker("image", "inspect", image).stdout)[0]["Config"]
    assert "/data" in (inspect.get("Volumes") or {})
    assert "MOEKA_WORKSPACE=/data/ws" in inspect["Env"]


def test_missing_secret_exits_2(image: str) -> None:
    result = _docker("run", "--rm", image)
    assert result.returncode == 2
    assert "MOEKA_TOKEN_ISSUE_SECRET" in result.stderr


def test_unwritable_data_names_data_and_uid(image: str, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o555)
    result = _docker("run", "--rm", "--user", "1000:1000", "-e", "MOEKA_TOKEN_ISSUE_SECRET=s",
                     "-v", f"{ro}:/data:ro", image)
    assert result.returncode != 0
    assert "/data" in result.stderr and "1000" in result.stderr


def test_first_start_seeds_the_config(image: str) -> None:
    name = f"moeka-005-seed-{uuid.uuid4().hex[:8]}"
    volume = f"{name}-data"
    try:
        result = _docker(
            "run", "--rm", "--name", name, "-e", "MOEKA_TOKEN_ISSUE_SECRET=s",
            "-v", f"{volume}:/data", "--entrypoint", "sh", image, "-c",
            "moeka-entrypoint --version >/dev/null 2>&1; cat /data/ws/config.json",
        )
        config = json.loads(result.stdout)
        assert config["agents"]["defaults"]["workspace"] == "/data/ws"
        assert config["gateway"]["host"] == "0.0.0.0" and config["gateway"]["port"] == 18790
        assert config["channels"]["websocket"] == {
            "host": "0.0.0.0", "port": 8765, "tokenIssueSecret": "${MOEKA_TOKEN_ISSUE_SECRET}",
        }
        assert config["providers"]["ollama"]["apiBase"] == "${MOEKA_OLLAMA_API_BASE}"
        again = _docker(
            "run", "--rm", "-e", "MOEKA_TOKEN_ISSUE_SECRET=s", "-v", f"{volume}:/data",
            "--entrypoint", "sh", image, "-c",
            "echo '{\"mine\": true}' > /data/ws/config.json; moeka-entrypoint --version "
            ">/dev/null 2>&1; cat /data/ws/config.json",
        )
        assert json.loads(again.stdout) == {"mine": True}  # never overwritten
    finally:
        _docker("volume", "rm", "-f", volume)


def test_two_compose_projects_run_side_by_side(image: str) -> None:
    suffix = uuid.uuid4().hex[:8]
    projects = [f"moeka005a{suffix}", f"moeka005b{suffix}"]
    ports = [(_free_port(), _free_port()) for _ in projects]
    base = {**os.environ, "MOEKA_TOKEN_ISSUE_SECRET": "secret-" + suffix, "MOEKA_IMAGE": image}
    try:
        for project, (gw, ws) in zip(projects, ports, strict=True):
            env = {**base, "MOEKA_GATEWAY_PORT": str(gw), "MOEKA_WS_PORT": str(ws)}
            up = _docker("compose", "-p", project, "up", "-d", "--no-build", env=env)
            assert up.returncode == 0, up.stderr
        for gw, _ws in ports:
            assert _health(gw), f"no /health on {gw}"
        volumes = _docker("volume", "ls", "--format", "{{.Name}}").stdout.split()
        assert all(f"{p}_moeka-data" in volumes for p in projects)
    finally:
        for project in projects:
            _docker("compose", "-p", project, "down", "-v", env=base)
