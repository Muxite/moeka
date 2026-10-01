"""Host-side instance discovery, status and port assignment (spec 005).

Stdlib only and free of ``nanobot`` imports: ``bin/moeka.sh`` runs this file by path
(``python3 -P nanobot/config/instances.py ...``) without the venv, and host Python code
(CLI, gateway, legacy floor adapter) imports it. One set of rules for all of them.

Terms (spec 005):

- An instance is identified by its root (the workspace directory, which in the flat
  layout also holds ``config.json``).
- Default instance: ``$HOME/.nanobot``. Named instance: ``$HOME/.moeka-<name>``.
  Registered instance: any other root listed in ``$HOME/.config/moeka/instances``.
- Discovered instances: the default one when its ``config.json`` exists, every
  ``$HOME/.moeka-*/config.json``, and every registry line whose ``config.json`` exists.
- Run dir: ``$MOEKA_RUN_DIR``, else ``$XDG_RUNTIME_DIR/moeka``, else ``/tmp/moeka-<uid>``.

Kernel code never imports this module: discovery is a host operation.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Schema defaults used when a config omits a port (``GatewayConfig``, ``ApiConfig``,
#: ``WebSocketConfig``).
DEFAULT_PORTS: dict[str, int] = {"gateway": 18790, "api": 8900, "websocket": 8765}

#: Candidate port bases for ``moeka.sh new`` without ``--port-base``.
PORT_BASES: tuple[int, ...] = tuple(range(18800, 19791, 10))

#: Longest Unix socket path ``bind(2)`` accepts on Linux (``sun_path`` is 108 bytes).
MAX_UNIX_SOCKET_PATH = 107

PID_FILENAME = "moeka.pid"
GATEWAY_LOCK_FILENAME = "gateway.lock"
LOG_FILENAME = "moeka.log"
SOCKET_RELPATH = ("run", "websocket.sock")


class InstanceError(Exception):
    """A refused instance operation; ``code`` is the CLI exit code (1 runtime, 2 usage)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# -- locations ---------------------------------------------------------------------


def home_dir(home: str | Path | None = None) -> Path:
    """``home`` or ``$HOME`` (never the password database when ``HOME`` is set)."""
    if home is not None:
        return Path(home)
    value = os.environ.get("HOME")
    return Path(value) if value else Path.home()


def registry_path(home: str | Path | None = None) -> Path:
    return home_dir(home) / ".config" / "moeka" / "instances"


def default_root(home: str | Path | None = None) -> Path:
    return home_dir(home) / ".nanobot"


def named_root(name: str, home: str | Path | None = None) -> Path:
    return home_dir(home) / f".moeka-{name}"


def run_dir(env: Mapping[str, str] | None = None, *, create: bool = True) -> Path:
    """The per-user run dir for cross-instance locks (created with mode 0700)."""
    source = os.environ if env is None else env
    explicit = source.get("MOEKA_RUN_DIR")
    if explicit:
        path = Path(explicit)
    elif source.get("XDG_RUNTIME_DIR"):
        path = Path(source["XDG_RUNTIME_DIR"]) / "moeka"
    else:
        path = Path(f"/tmp/moeka-{os.getuid()}")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            if path.stat().st_uid == os.getuid():
                os.chmod(path, 0o700)
    return path


def _abs(path: str | Path) -> Path:
    """Absolute, ``~``-expanded, normalised (symlinks are not resolved)."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def instance_kind(root: str | Path, home: str | Path | None = None) -> str:
    """``"default"``, ``"named"`` or ``"registered"`` for an instance root."""
    root_path = _abs(root)
    home_path = _abs(home_dir(home))
    if root_path == home_path / ".nanobot":
        return "default"
    if root_path.parent == home_path and root_path.name.startswith(".moeka-"):
        if NAME_RE.match(root_path.name[len(".moeka-"):]):
            return "named"
    return "registered"


def instance_name(root: str | Path, home: str | Path | None = None) -> str:
    root_path = _abs(root)
    kind = instance_kind(root_path, home)
    if kind == "default":
        return "default"
    if kind == "named":
        return root_path.name[len(".moeka-"):]
    return root_path.name


def unit_for(root: str | Path, home: str | Path | None = None) -> str | None:
    """The instance's own systemd user unit (``None`` for a registered instance)."""
    kind = instance_kind(root, home)
    if kind == "default":
        return "moeka.service"
    if kind == "named":
        return f"moeka@{instance_name(root, home)}.service"
    return None


def socket_path(root: str | Path) -> Path:
    return _abs(root).joinpath(*SOCKET_RELPATH)


# -- config reading ------------------------------------------------------------------


def read_config(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(data: Mapping[str, Any], *names: str) -> dict[str, Any]:
    current: Any = data
    for name in names:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(name)
    return dict(current) if isinstance(current, Mapping) else {}


def _get(section: Mapping[str, Any], camel: str, snake: str | None = None) -> Any:
    if camel in section:
        return section[camel]
    if snake is not None and snake in section:
        return section[snake]
    return None


def _port(value: Any, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def config_ports(data: Mapping[str, Any]) -> tuple[dict[str, int | None], str | None]:
    """``({"gateway", "api", "websocket"}, websocket_socket)`` with schema defaults.

    A WebSocket channel listening on a Unix socket uses no TCP port (``None``).
    """
    gateway = _section(data, "gateway")
    api = _section(data, "api")
    ws = _section(data, "channels", "websocket")
    sock = _get(ws, "unixSocketPath", "unix_socket_path")
    sock = sock.strip() if isinstance(sock, str) else ""
    ports: dict[str, int | None] = {
        "gateway": _port(gateway.get("port"), DEFAULT_PORTS["gateway"]),
        "api": _port(api.get("port"), DEFAULT_PORTS["api"]),
        "websocket": None if sock else _port(ws.get("port"), DEFAULT_PORTS["websocket"]),
    }
    return ports, (sock or None)


# -- discovery -----------------------------------------------------------------------


def read_registry(home: str | Path | None = None) -> list[Path]:
    try:
        lines = registry_path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[Path] = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and os.path.isabs(line):
            out.append(Path(line))
    return out


def register(root: str | Path, home: str | Path | None = None) -> None:
    """Append *root* (absolute) to the registry unless it is already listed."""
    root_path = _abs(root)
    if root_path in {_abs(p) for p in read_registry(home)}:
        return
    path = registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{root_path}\n")


def discover_roots(home: str | Path | None = None) -> list[Path]:
    """Roots of every discovered instance (default first, then named, then registered)."""
    home_path = home_dir(home)
    roots: list[Path] = []
    seen: set[str] = set()

    def add(root: Path) -> None:
        root = _abs(root)
        if not (root / "config.json").is_file():
            return
        key = os.path.realpath(root)
        if key in seen:
            return
        seen.add(key)
        roots.append(root)

    add(home_path / ".nanobot")
    with contextlib.suppress(OSError):
        for child in sorted(home_path.glob(".moeka-*")):
            if child.is_dir() and NAME_RE.match(child.name[len(".moeka-"):]):
                add(child)
    for root in read_registry(home_path):
        add(root)
    return roots


def other_instance_roots(own: str | Path | None, home: str | Path | None = None) -> list[Path]:
    """Discovered roots that neither equal nor contain *own* (the agent's work dir)."""
    own_path = Path(os.path.realpath(_abs(own))) if own is not None else None
    out: list[Path] = []
    for root in discover_roots(home):
        real = Path(os.path.realpath(root))
        if own_path is not None and (own_path == real or own_path.is_relative_to(real)):
            continue
        out.append(root)
    return out


# -- liveness ------------------------------------------------------------------------


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 0 else None


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]


def pid_matches(pid: int, config: str | Path) -> bool:
    """True when ``/proc/<pid>/cmdline`` contains *config* (FR-004b).

    As an argument, after ``=`` (``--config=PATH``), or as a word inside one argument;
    the symlink-resolved path counts too.
    """
    wanted = {str(config), os.path.realpath(str(config))}
    for arg in _cmdline(pid):
        for path in wanted:
            if arg == path or arg.endswith("=" + path) or path in arg.replace("=", " ").split():
                return True
    return False


def live_pid(root: str | Path, config: str | Path) -> int | None:
    """The PID in ``<root>/moeka.pid`` when it is this instance's live gateway."""
    pid = _read_pid(_abs(root) / PID_FILENAME)
    if pid is None:
        return None
    return pid if pid_matches(pid, config) else None


def lock_held(path: str | Path) -> bool:
    """True when another open file description holds a ``flock`` on *path*."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def systemctl_command(env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    value = source.get("MOEKA_SYSTEMCTL") or "systemctl"
    return shlex.split(value) or ["systemctl"]


def unit_active(unit: str | None, env: Mapping[str, str] | None = None) -> bool:
    if not unit:
        return False
    command = [*systemctl_command(env), "--user", "is-active", "--quiet", unit]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def describe(
    root: str | Path,
    *,
    config: str | Path | None = None,
    home: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The FR-006 status object for one instance."""
    root_path = _abs(root)
    config_path = _abs(config) if config else root_path / "config.json"
    data = read_config(config_path)
    ports, sock = config_ports(data)
    unit = unit_for(root_path, home)
    active = unit_active(unit, env)
    pid = live_pid(root_path, config_path)
    locked = lock_held(root_path / GATEWAY_LOCK_FILENAME)
    manager: str | None
    if active:
        manager = "systemd"
    elif pid is not None or locked:
        manager = "pid"
    else:
        manager = None
    if pid is None and locked:
        pid = _read_pid(root_path / PID_FILENAME)
    return {
        "name": instance_name(root_path, home),
        "workspace": str(root_path),
        "config": str(config_path),
        "running": bool(active or pid is not None or locked),
        "pid": pid,
        "manager": manager,
        "unit": unit,
        "ports": ports,
        "websocket_socket": sock,
    }


def list_instances(
    home: str | Path | None = None, env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [describe(root, home=home, env=env) for root in discover_roots(home)]


# -- port assignment -----------------------------------------------------------------


def used_ports(roots: list[Path]) -> dict[int, Path]:
    """Every TCP port a discovered instance's config uses, mapped to its root."""
    used: dict[int, Path] = {}
    for root in roots:
        ports, _sock = config_ports(read_config(root / "config.json"))
        for value in ports.values():
            if value is not None:
                used.setdefault(value, root)
    return used


def port_bindable(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def needed_ports(base: int, ws_tcp: bool) -> list[int]:
    return [base, base + 1, *([base + 2] if ws_tcp else [])]


def choose_port_base(
    roots: list[Path],
    *,
    ws_tcp: bool = False,
    port_base: int | None = None,
) -> int:
    """The port base for a new instance (FR-016, FR-017); raises :class:`InstanceError`."""
    used = used_ports(roots)
    if port_base is not None:
        if isinstance(port_base, bool) or not 1024 <= port_base <= 65533:
            raise InstanceError(
                f"--port-base must be an integer in [1024, 65533], got {port_base}", 2,
            )
        if ws_tcp and port_base + 2 > 65535:
            raise InstanceError(f"--port-base {port_base} leaves no room for --ws-tcp", 2)
        for port in needed_ports(port_base, ws_tcp):
            if port in used:
                raise InstanceError(
                    f"port {port} is already assigned to instance {used[port]}", 1,
                )
            if not port_bindable(port):
                raise InstanceError(f"port {port} is in use (cannot bind 127.0.0.1:{port})", 1)
        return port_base
    for base in PORT_BASES:
        ports = needed_ports(base, ws_tcp)
        if any(port in used for port in ports):
            continue
        if all(port_bindable(port) for port in ports):
            return base
    raise InstanceError("no free port base", 1)


@contextlib.contextmanager
def new_lock(env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Serialise port selection and config writing of concurrent ``new`` runs."""
    path = run_dir(env) / "new.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def create_instance(
    name: str,
    *,
    template: str | Path,
    workspace: str | Path | None = None,
    port_base: int | None = None,
    ws_tcp: bool = False,
    keys_example: str | Path | None = None,
    home: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Scaffold a new instance (FR-013..FR-020). Returns its FR-006 description."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise InstanceError(
            f"invalid instance name {name!r}: must match ^[a-z0-9][a-z0-9_-]{{0,31}}$", 2,
        )
    if port_base is not None and (
        isinstance(port_base, bool) or not 1024 <= port_base <= 65533
    ):
        raise InstanceError(
            f"--port-base must be an integer in [1024, 65533], got {port_base}", 2,
        )
    home_path = home_dir(home)
    target = _abs(workspace) if workspace else named_root(name, home_path)
    if "${" in str(target):
        raise InstanceError(f"MOEKA_WORKSPACE/--workspace has an unexpanded variable: {target}", 2)
    sock = target.joinpath(*SOCKET_RELPATH)
    if not ws_tcp and len(os.fsencode(str(sock))) > MAX_UNIX_SOCKET_PATH:
        raise InstanceError(
            f"unix socket path too long ({len(os.fsencode(str(sock)))} > "
            f"{MAX_UNIX_SOCKET_PATH} bytes): {sock}; use --ws-tcp for a TCP WebSocket port",
            2,
        )
    template_path = Path(template)
    if not (template_path / "config.json").is_file():
        raise InstanceError(f"templates not found: {template_path}", 1)

    def not_empty() -> bool:
        if target.is_dir():
            return any(target.iterdir())
        return target.exists()

    if not_empty():
        raise InstanceError(f"target not empty: {target}", 1)

    with new_lock(env):
        if not_empty():
            raise InstanceError(f"target not empty: {target}", 1)
        roots = [r for r in discover_roots(home_path) if _abs(r) != target]
        base = choose_port_base(roots, ws_tcp=ws_tcp, port_base=port_base)

        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_path, target, dirs_exist_ok=True)
        for filename in ("SOUL.md", "USER.md"):
            file = target / filename
            if file.is_file():
                text = file.read_text(encoding="utf-8")
                file.write_text(
                    text.replace("{{NAME}}", name).replace("{{USER_NAME}}", name),
                    encoding="utf-8",
                )

        config_file = target / "config.json"
        data = read_config(config_file)
        agents = data.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults["workspace"] = str(target)
        data.setdefault("gateway", {})["port"] = base
        data.setdefault("api", {})["port"] = base + 1
        channels = data.setdefault("channels", {})
        websocket = channels.setdefault("websocket", {})
        if not isinstance(websocket, dict):
            websocket = channels["websocket"] = {}
        websocket.pop("unix_socket_path", None)
        if ws_tcp:
            websocket["host"] = "127.0.0.1"
            websocket["port"] = base + 2
            websocket["unixSocketPath"] = ""
        else:
            websocket.pop("port", None)
            websocket["unixSocketPath"] = str(sock)
            sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(sock.parent, 0o700)
        _write_json_atomic(config_file, data)

        keys = target / "keys.env"
        content = ""
        if keys_example is not None and Path(keys_example).is_file():
            content = Path(keys_example).read_text(encoding="utf-8")
        fd = os.open(str(keys), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(keys, 0o600)

        if instance_kind(target, home_path) == "registered":
            register(target, home_path)
    info = describe(target, home=home_path, env=env)
    return info


# -- CLI -----------------------------------------------------------------------------


def _human_status(info: Mapping[str, Any]) -> str:
    ports = info["ports"]
    lines = [
        f"name         : {info['name']}",
        f"workspace    : {info['workspace']}",
        f"config file  : {info['config']}",
        f"running      : {'yes' if info['running'] else 'no'}",
        f"pid          : {info['pid'] if info['pid'] is not None else '-'}",
        f"manager      : {info['manager'] or '-'}",
        f"unit         : {info['unit'] or '-'}",
        f"gateway port : {ports['gateway']}",
        f"api port     : {ports['api']}",
        f"websocket    : {info['websocket_socket'] or ports['websocket']}",
    ]
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="instances.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")
    p_status = sub.add_parser("status")
    p_status.add_argument("--root", required=True)
    p_status.add_argument("--config")
    p_status.add_argument("--json", action="store_true")
    p_ports = sub.add_parser("ports")
    p_ports.add_argument("--json", action="store_true")
    p_new = sub.add_parser("new")
    p_new.add_argument("name")
    p_new.add_argument("--workspace")
    p_new.add_argument("--port-base")
    p_new.add_argument("--ws-tcp", action="store_true")
    p_new.add_argument("--template", required=True)
    p_new.add_argument("--keys-example")
    p_kind = sub.add_parser("kind")
    p_kind.add_argument("root")
    p_discover = sub.add_parser("roots")
    p_discover.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "list":
            items = list_instances()
            if args.json:
                print(json.dumps(items))
            else:
                for item in items:
                    state = f"running ({item['manager']}, pid {item['pid']})" \
                        if item["running"] else "stopped"
                    print(f"{item['name']}\t{item['workspace']}\t{state}")
            return 0
        if args.cmd == "status":
            info = describe(args.root, config=args.config)
            print(json.dumps(info) if args.json else _human_status(info))
            return 0 if info["running"] else 3
        if args.cmd == "ports":
            used = used_ports(discover_roots())
            payload = {str(port): str(root) for port, root in sorted(used.items())}
            print(json.dumps(payload) if args.json else "\n".join(
                f"{port}\t{root}" for port, root in payload.items()
            ))
            return 0
        if args.cmd == "kind":
            root = args.root
            print(f"{instance_kind(root)}\t{instance_name(root)}\t{unit_for(root) or ''}")
            return 0
        if args.cmd == "roots":
            roots = [str(r) for r in discover_roots()]
            print(json.dumps(roots) if args.json else "\n".join(roots))
            return 0
        if args.cmd == "new":
            port_base: int | None = None
            if args.port_base is not None:
                if not re.fullmatch(r"[0-9]+", args.port_base.strip()):
                    raise InstanceError(
                        f"--port-base must be an integer in [1024, 65533], got {args.port_base!r}",
                        2,
                    )
                port_base = int(args.port_base)
            info = create_instance(
                args.name, template=args.template, workspace=args.workspace,
                port_base=port_base, ws_tcp=args.ws_tcp, keys_example=args.keys_example,
            )
            ports = info["ports"]
            print(f"instance     : {info['name']}")
            print(f"workspace    : {info['workspace']}")
            print(f"config       : {info['config']}")
            print(f"gateway port : {ports['gateway']}")
            print(f"api port     : {ports['api']}")
            if info["websocket_socket"]:
                print(f"websocket    : unix socket {info['websocket_socket']}")
            else:
                print(f"websocket    : 127.0.0.1:{ports['websocket']}")
            return 0
    except InstanceError as exc:
        print(f"[moeka] {exc}", file=sys.stderr)
        return exc.code
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
