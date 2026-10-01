"""Group K: containers (FR-056..FR-059, US6, SC-001 partial).

Docker tests are marked `docker`, skip when docker is unavailable, use unique names/ports
and remove their containers, volumes and images.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from conftest import REPO, free_port

DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "compose.yaml"
ENTRY = REPO / "scripts" / "container-entrypoint.sh"
DEPLOY_DOC = REPO / "docs" / "deployment.md"


def _instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile instructions as (KEYWORD, args), joining backslash continuations and
    dropping comment lines (default escape character)."""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if buf and line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        if buf.strip():
            kw, _, args = buf.strip().partition(" ")
            out.append((kw.upper(), args.strip()))
        buf = ""
    if buf.strip():
        kw, _, args = buf.strip().partition(" ")
        out.append((kw.upper(), args.strip()))
    return out


def _env_pairs(args: str) -> dict[str, str]:
    """Parse ENV args: `K=V K2="V 2"` or the legacy `K V` form."""
    import shlex

    parts = shlex.split(args)
    if parts and "=" not in parts[0]:
        return {parts[0]: " ".join(parts[1:])}
    return dict(p.split("=", 1) for p in parts if "=" in p)


def _docker_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env["HOME"] = os.environ.get("HELDOUT_DOCKER_HOME", env.get("HOME", "/tmp"))
    for k in ("MOEKA_TOKEN_ISSUE_SECRET", "MOEKA_OLLAMA_API_BASE", "MOEKA_GATEWAY_PORT",
              "MOEKA_WS_PORT"):
        env.pop(k, None)
    return env


def docker(*args: str, timeout: float = 300, env: dict | None = None, cwd: Path | None = None):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, env=env or _docker_env(),
                              cwd=str(cwd or REPO), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, -999, exc.stdout or "", exc.stderr or "")


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return docker("info", "--format", "{{.ServerVersion}}", timeout=30).returncode == 0


def _health(port: int) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_health(port: int, timeout: float = 240) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port):
            return True
        time.sleep(1)
    return False


# -- static checks ---------------------------------------------------------------------------------


@pytest.mark.fr("FR-056")
def test_dockerfile_static():
    text = DOCKERFILE.read_text()
    ins = _instructions(text)
    users = [a.split()[0] for k, a in ins if k == "USER" and a]
    assert users and users[-1] in ("1000:1000", "moeka", "1000"), users
    env: dict[str, str] = {}
    for k, a in ins:
        if k == "ENV":
            env.update(_env_pairs(a))
    assert env.get("MOEKA_WORKSPACE") == "/data/ws", env
    assert any(k == "VOLUME" and re.search(r"(^|[\s\[\"'])/data([\s\]\"']|$)", a)
               for k, a in ins), "VOLUME /data missing"
    assert "container-entrypoint.sh" in text
    assert re.search(r"/data/ws/config\.json", text)
    for pat in (r"sk-[A-Za-z0-9]{16,}", r"(API_KEY|TOKEN|SECRET)\s*[= ]\s*['\"]?[A-Za-z0-9_\-]{12,}"):
        assert not re.search(pat, text), f"secret-like value in Dockerfile: {pat}"
    assert ENTRY.exists()


@pytest.mark.fr("FR-058")
def test_compose_static():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(COMPOSE.read_text())
    services = doc["services"]
    assert len(services) == 1
    (svc,) = services.values()
    assert str(svc.get("user")) == "1000:1000"
    vols = svc.get("volumes") or []
    named = [v for v in vols if isinstance(v, str) and v.split(":")[1:2] == ["/data"]
             and not v.startswith((".", "/"))] + \
        [v for v in vols if isinstance(v, dict) and v.get("target") == "/data"
         and v.get("type", "volume") == "volume"]
    assert named, vols
    assert doc.get("volumes"), "the named volume must be declared"
    ports = [str(p) for p in svc.get("ports") or []]
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), ports
    joined = " ".join(ports)
    assert "${MOEKA_GATEWAY_PORT:-18790}" in joined and "${MOEKA_WS_PORT:-8765}" in joined
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) if "=" in e else (e, None) for e in env)
    assert "${MOEKA_TOKEN_ISSUE_SECRET:?" in str(env.get("MOEKA_TOKEN_ISSUE_SECRET"))
    assert "MOEKA_OLLAMA_API_BASE" in env
    assert any("host.docker.internal:host-gateway" in str(x) or
               "host.docker.internal=host-gateway" in str(x)
               for x in (svc.get("extra_hosts") or []))
    assert "ALL" in [str(c).upper() for c in svc.get("cap_drop") or []]
    assert any(str(o).startswith("no-new-privileges") for o in svc.get("security_opt") or [])
    hc = svc.get("healthcheck") or {}
    assert "/health" in json.dumps(hc.get("test"))


@pytest.mark.fr("FR-059")
def test_deployment_doc():
    text = DEPLOY_DOC.read_text()
    for stale in ("docker-compose.yml", "docker-compose.bwrap.yml", "/home/nanobot/.nanobot",
                  "nanobot-gateway", "nanobot-cli", "NANOBOT_CHANNELS", "NANOBOT_EXTRAS"):
        if stale.endswith(".yml") and (REPO / stale).exists():
            continue
        assert stale not in text, stale
    for needed in ("compose.yaml", "0.0.0.0", "tokenIssueSecret", "MOEKA_OLLAMA_API_BASE",
                   "CAP_SYS_ADMIN", "-p "):
        assert needed in text, needed
    assert re.search(r"user namespace", text, re.I)


# -- docker ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def image():
    if not _docker_ok():
        pytest.skip("docker daemon not available")
    tag = f"moeka-heldout-005:{uuid.uuid4().hex[:10]}"
    r = docker("build", "-t", tag, ".", timeout=2400)
    if r.returncode != 0:
        pytest.fail(f"docker build failed: {r.stderr[-3000:]}")
    yield tag
    docker("image", "rm", "-f", tag, timeout=120)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for n in names:
        docker("rm", "-f", "-v", n, timeout=120)


@pytest.mark.docker
@pytest.mark.fr("FR-056")
@pytest.mark.timeout(2700)
def test_image_runs_as_1000(image):
    r = docker("run", "--rm", "--entrypoint", "id", image, "-u", timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "1000", r.stderr
    r = docker("run", "--rm", "--entrypoint", "id", image, "-g", timeout=120)
    assert r.stdout.strip() == "1000"
    r = docker("image", "inspect", image, "--format", "{{json .Config}}", timeout=60)
    cfg = json.loads(r.stdout)
    assert "MOEKA_WORKSPACE=/data/ws" in (cfg.get("Env") or [])
    assert "/data" in (cfg.get("Volumes") or {})


@pytest.mark.docker
@pytest.mark.fr("FR-057")
@pytest.mark.timeout(2700)
@pytest.mark.parametrize("secret", [None, ""])
def test_missing_secret_exits_2(image, containers, secret):
    name = f"mhd-{uuid.uuid4().hex[:10]}"
    containers.append(name)
    args = ["run", "--name", name, "-p", f"127.0.0.1:{free_port()}:18790"]
    if secret is not None:
        args += ["-e", f"MOEKA_TOKEN_ISSUE_SECRET={secret}"]
    r = docker(*args, image, timeout=180)
    assert r.returncode == 2, (r.returncode, r.stderr[-2000:])


@pytest.mark.docker
@pytest.mark.fr("FR-057")
@pytest.mark.timeout(2700)
def test_unwritable_data_exits_nonzero(image, containers, h):
    data = h.aux / "ro-data"
    data.mkdir()
    data.chmod(0o555)
    name = f"mhd-{uuid.uuid4().hex[:10]}"
    containers.append(name)
    r = docker("run", "--name", name, "-e", "MOEKA_TOKEN_ISSUE_SECRET=heldout-secret",
               "-v", f"{data}:/data", image, timeout=180)
    assert r.returncode not in (0, -999)
    out = r.stdout + r.stderr
    assert "/data" in out and "1000" in out


@pytest.mark.docker
@pytest.mark.fr("FR-057", "FR-056")
@pytest.mark.timeout(2700)
def test_first_start_seeds_config_and_never_overwrites(image, containers, h):
    data = h.aux / "data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    port = free_port()
    name = f"mhd-{uuid.uuid4().hex[:10]}"
    containers.append(name)
    r = docker("run", "-d", "--name", name, "-e", "MOEKA_TOKEN_ISSUE_SECRET=heldout-secret",
               "-v", f"{data}:/data", "-p", f"127.0.0.1:{port}:18790", image, timeout=120)
    assert r.returncode == 0, r.stderr
    assert _wait_health(port), docker("logs", name).stdout[-3000:]
    cfg = json.loads((data / "ws" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["workspace"] == "/data/ws"
    assert cfg["gateway"]["host"] == "0.0.0.0" and cfg["gateway"]["port"] == 18790
    ws = cfg["channels"]["websocket"]
    assert ws["host"] == "0.0.0.0" and ws["port"] == 8765
    assert ws["tokenIssueSecret"] == "${MOEKA_TOKEN_ISSUE_SECRET}"
    assert cfg["providers"]["ollama"]["apiBase"] == "${MOEKA_OLLAMA_API_BASE}"
    assert (data / "ws-sessions").is_dir()
    env = docker("exec", name, "cat", "/proc/1/environ").stdout
    assert "MOEKA_OLLAMA_API_BASE=http://host.docker.internal:11434/v1" in env.split("\0")
    docker("rm", "-f", name, timeout=120)
    marker = json.loads((data / "ws" / "config.json").read_text())
    marker["heldoutMarker"] = "keep-me"
    (data / "ws" / "config.json").write_text(json.dumps(marker, indent=2))
    before = (data / "ws" / "config.json").read_bytes()
    name2 = f"mhd-{uuid.uuid4().hex[:10]}"
    containers.append(name2)
    r = docker("run", "-d", "--name", name2, "-e", "MOEKA_TOKEN_ISSUE_SECRET=heldout-secret",
               "-v", f"{data}:/data", image, timeout=120)
    assert r.returncode == 0
    time.sleep(8)
    assert (data / "ws" / "config.json").read_bytes() == before


def _compose(project: str, *args: str, env: dict, timeout: float = 1800):
    return docker("compose", "-p", project, "-f", str(COMPOSE), *args, env=env, timeout=timeout)


@pytest.mark.docker
@pytest.mark.fr("FR-058")
@pytest.mark.timeout(120)
def test_compose_requires_secret():
    if not _docker_ok():
        pytest.skip("docker daemon not available")
    r = _compose(f"mhd{uuid.uuid4().hex[:8]}", "config", env=_docker_env(), timeout=60)
    assert r.returncode != 0
    assert "MOEKA_TOKEN_ISSUE_SECRET" in r.stdout + r.stderr


@pytest.mark.docker
@pytest.mark.fr("FR-058", "SC-001")
@pytest.mark.timeout(3600)
def test_two_compose_projects_side_by_side():
    if not _docker_ok():
        pytest.skip("docker daemon not available")
    projects = []
    try:
        for _ in range(2):
            p = f"mhd{uuid.uuid4().hex[:8]}"
            env = _docker_env()
            env.update(MOEKA_GATEWAY_PORT=str(free_port()), MOEKA_WS_PORT=str(free_port()),
                       MOEKA_TOKEN_ISSUE_SECRET=f"secret-{p}")
            projects.append((p, env))
            r = _compose(p, "up", "-d", env=env)
            assert r.returncode == 0, r.stderr[-3000:]
        for p, env in projects:
            assert _wait_health(int(env["MOEKA_GATEWAY_PORT"])), p
        time.sleep(5)
        for p, env in projects:
            assert _health(int(env["MOEKA_GATEWAY_PORT"]))
        vols = []
        for p, _ in projects:
            r = docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={p}")
            names = [x for x in r.stdout.split() if x]
            assert names, p
            vols.append(set(names))
        assert not vols[0] & vols[1]
        for p, env in projects:
            r = docker("ps", "--filter", f"label=com.docker.compose.project={p}",
                       "--format", "{{.Ports}}")
            ports = r.stdout
            assert f"127.0.0.1:{env['MOEKA_GATEWAY_PORT']}->" in ports, ports
            assert "0.0.0.0:" not in ports and ":::" not in ports, ports
    finally:
        for p, env in projects:
            _compose(p, "down", "-v", "--rmi", "local", env=env, timeout=600)
