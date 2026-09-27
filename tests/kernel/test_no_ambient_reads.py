"""Invariant I1 proof: no ambient reads outside the host-adapter allow-list.

Every ``nanobot/**/*.py`` and ``moeka/**/*.py`` is parsed with :mod:`ast` (so docstrings and comments
never match) and scanned for reads of process-global state: the process
environment, the user's home directory, and the ambient config/state locations.
Kernel code must receive all of these through ``CoreEnvironment``; only the host
adapters in :data:`AMBIENT_ALLOWLIST` may touch them.

The scanner has its own self-test (``test_scanner_*``): each forbidden pattern
must be flagged and each allowed pattern must not.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "nanobot"
# The public facade: re-exports only, so it gets no allow-list entries at all.
PACKAGE_ROOTS = (PACKAGE_ROOT, REPO_ROOT / "moeka")

# Host adapters where ambient reads are allowed (plan ruling R2, amended by Ruling G).
AMBIENT_ALLOWLIST: dict[str, str] = {
    # CLI entry points: the host itself (reads HOME, env, config.json to build the env).
    "nanobot/cli/": "host CLI: builds the environment from process state",
    # Whole config layer: paths.py (get_state_home/get_data_dir), loader.py
    # (get_config_path/load_config, ${VAR} expansion), schema.py (workspace_path
    # get_state_home() fallback, pydantic env_prefix="NANOBOT_" settings).
    "nanobot/config/": "config layer: resolves config.json, state home and ${VAR}",
    # The ambient adapter: LegacyEnvironment and the legacy env=None fallbacks.
    "nanobot/kernel/legacy.py": "ambient adapter (LegacyEnvironment + legacy helpers)",
    # Process restart helper: re-execs the host with its own environment.
    "nanobot/utils/restart.py": "host process restart (re-exec with process env)",
}

# Per-file exemptions outside the allow-list: each needs a written reason. Keep tiny.
KNOWN_EXEMPTIONS: dict[str, str] = {
    # abbreviate_path() reads the home directory only to shorten a path string for
    # display in tool-call hints ("/home/u/x" -> "~/x"). The value never reaches a
    # filesystem operation, a credential, or a prompt as data the kernel acts on.
    "nanobot/utils/path.py": "display-only home abbreviation in tool hints",
}

_OS_ENV_ATTRS = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
# Ambient config/state locators (nanobot/config/{paths,loader}.py). The first four
# are Ruling G's list; the rest derive from them (get_state_home/get_data_dir) and
# are just as ambient, so kernel modules reach all of them only via legacy helpers.
_AMBIENT_FUNCS = frozenset({
    "load_config",
    "get_state_home",
    "get_data_dir",
    "get_config_path",
    "get_runtime_subdir",
    "get_media_dir",
    "get_cron_dir",
    "get_logs_dir",
    "get_workspace_path",
    "get_cli_history_path",
    "get_bridge_install_dir",
    "get_legacy_sessions_dir",
})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def _is_home_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "~" or node.value.startswith("~/") or node.value.startswith("~\\"))
    )


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self.os_names: set[str] = set()  # names bound to the os module
        self.ospath_names: set[str] = set()  # names bound to os.path
        self.env_names: set[str] = set()  # names bound to os.environ / getenv / ...
        self.expanduser_names: set[str] = set()  # bare os.path.expanduser aliases
        self.expandvars_names: set[str] = set()
        self.path_names: set[str] = {"Path"}  # pathlib.Path (and aliases)
        self.pathlib_names: set[str] = set()
        self.func_aliases: set[str] = set()  # aliases of _AMBIENT_FUNCS

    def flag(self, node: ast.AST, what: str) -> None:
        self.violations.append(Violation(self.path, getattr(node, "lineno", 0), what))

    # --- imports -------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os":
                self.os_names.add(alias.asname or "os")
            elif alias.name == "os.path":
                if alias.asname:
                    self.ospath_names.add(alias.asname)
                else:
                    self.os_names.add("os")
            elif alias.name == "pathlib":
                self.pathlib_names.add(alias.asname or "pathlib")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if module == "os":
                if alias.name == "*":
                    self.flag(node, "from os import *")
                elif alias.name in _OS_ENV_ATTRS:
                    self.env_names.add(bound)
                    self.flag(node, f"from os import {alias.name}")
                elif alias.name == "path":
                    self.ospath_names.add(bound)
            elif module in {"os.path", "posixpath", "ntpath"}:
                if alias.name == "expanduser":
                    self.expanduser_names.add(bound)
                elif alias.name == "expandvars":
                    self.expandvars_names.add(bound)
                    self.flag(node, "from os.path import expandvars")
            elif module == "pathlib" and alias.name == "Path":
                self.path_names.add(bound)
            elif alias.name in _AMBIENT_FUNCS:
                self.func_aliases.add(bound)
                self.flag(node, f"import of ambient helper {alias.name}")
        self.generic_visit(node)

    # --- helpers -------------------------------------------------------------
    def _is_os(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.os_names

    def _is_ospath(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.ospath_names
        return isinstance(node, ast.Attribute) and node.attr == "path" and self._is_os(node.value)

    def _is_path_class(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.path_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "Path"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.pathlib_names
        )

    # --- expressions ---------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _OS_ENV_ATTRS and self._is_os(node.value):
            self.flag(node, f"os.{node.attr}")
        elif (
            node.attr == "get"
            and isinstance(node.value, ast.Name)
            and (node.value.id == "environ" or node.value.id in self.env_names)
        ):
            self.flag(node, "environ.get")
        elif node.attr in _AMBIENT_FUNCS:
            self.flag(node, f"ambient helper .{node.attr}")
        elif node.attr == "expandvars" and self._is_ospath(node.value):
            self.flag(node, "os.path.expandvars (reads the process environment)")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if node.id in _AMBIENT_FUNCS or node.id in self.func_aliases:
                self.flag(node, f"ambient helper {node.id}")
            elif node.id in self.env_names:
                self.flag(node, f"os environment alias {node.id}")
            elif node.id in self.expandvars_names:
                self.flag(node, "expandvars (reads the process environment)")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # getattr(os, "environ") and friends.
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and self._is_os(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _OS_ENV_ATTRS
        ):
            self.flag(node, f'getattr(os, "{node.args[1].value}")')
        # Path.home() / pathlib.Path.home()
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "home"
            and self._is_path_class(func.value)
        ):
            self.flag(node, "Path.home()")
        # os.path.expanduser("~...") or a bare expanduser alias.
        is_os_expanduser = (
            isinstance(func, ast.Attribute)
            and func.attr == "expanduser"
            and self._is_ospath(func.value)
        ) or (isinstance(func, ast.Name) and func.id in self.expanduser_names)
        if is_os_expanduser:
            if node.args and _is_home_literal(node.args[0]):
                self.flag(node, 'os.path.expanduser("~")')
        elif isinstance(func, ast.Name) and func.id == "expanduser" and not node.args:
            self.flag(node, "expanduser() with no argument")
        # Path("~...").expanduser(): expands a literal home path, not a caller's path.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "expanduser"
            and isinstance(func.value, ast.Call)
            and self._is_path_class(func.value.func)
            and func.value.args
            and _is_home_literal(func.value.args[0])
        ):
            self.flag(node, 'Path("~").expanduser()')
        self.generic_visit(node)


def scan_source(source: str, path: str = "<src>") -> list[Violation]:
    scanner = _Scanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.violations


def _allowed(rel: str) -> bool:
    return any(rel == entry or (entry.endswith("/") and rel.startswith(entry))
               for entry in AMBIENT_ALLOWLIST)


def scan_package() -> list[Violation]:
    found: list[Violation] = []
    files = sorted(f for root in PACKAGE_ROOTS for f in root.rglob("*.py"))
    for file in files:
        rel = file.relative_to(REPO_ROOT).as_posix()
        if _allowed(rel) or rel in KNOWN_EXEMPTIONS:
            continue
        found.extend(scan_source(file.read_text(encoding="utf-8"), rel))
    return found


# --- the guard ---------------------------------------------------------------


def test_no_ambient_reads_outside_allowlist() -> None:
    violations = scan_package()
    assert not violations, (
        "ambient reads outside the R2 allow-list (route them through CoreEnvironment "
        "or a nanobot/kernel/legacy.py helper):\n" + "\n".join(map(str, violations))
    )


def test_scan_covers_the_moeka_facade() -> None:
    assert (REPO_ROOT / "moeka" / "__init__.py").exists()
    assert not any(entry.startswith("moeka/") for entry in [*AMBIENT_ALLOWLIST, *KNOWN_EXEMPTIONS])


def test_allowlist_and_exemptions_exist() -> None:
    """A stale entry would silently widen the guard; every entry must name real code."""
    for entry in [*AMBIENT_ALLOWLIST, *KNOWN_EXEMPTIONS]:
        assert (REPO_ROOT / entry).exists(), entry
    assert len(KNOWN_EXEMPTIONS) <= 2, "exemption list must stay tiny"


def test_exemptions_are_still_needed() -> None:
    """An exemption whose file became clean must be removed."""
    for rel in KNOWN_EXEMPTIONS:
        assert scan_source((REPO_ROOT / rel).read_text(encoding="utf-8"), rel), rel


# --- scanner self-test --------------------------------------------------------

FORBIDDEN_SNIPPETS = {
    "os.environ": "import os\nx = os.environ['HOME']\n",
    "os.environ.get": "import os\nx = os.environ.get('HOME')\n",
    "os.getenv": "import os\nx = os.getenv('HOME')\n",
    "os.putenv": "import os\nos.putenv('A', 'b')\n",
    "os.environb": "import os\nx = os.environb\n",
    "import os as alias": "import os as _os\nx = _os.environ\n",
    "import os.path": "import os.path\nx = os.environ\n",
    "from os import environ": "from os import environ\n",
    "from os import environ as e": "from os import environ as e\nx = e['A']\n",
    "from os import getenv": "from os import getenv as g\nx = g('A')\n",
    "bare environ.get": "x = environ.get('A')\n",
    "getattr(os, environ)": "import os\nx = getattr(os, 'environ')\n",
    "getattr(alias, getenv)": "import os as o\nx = getattr(o, 'getenv')('A')\n",
    "load_config(": "from nanobot.config.loader import load_config\nc = load_config()\n",
    "loader.load_config(": "from nanobot.config import loader\nc = loader.load_config()\n",
    "get_state_home(": "from nanobot.config import paths\nh = paths.get_state_home()\n",
    "get_data_dir(": "from nanobot.config.paths import get_data_dir\nd = get_data_dir()\n",
    "get_data_dir alias": "from nanobot.config.paths import get_data_dir as g\nd = g()\n",
    "get_config_path(": "import nanobot.config.loader as L\np = L.get_config_path()\n",
    "helper as callback": "import nanobot.config.paths as p\nf(default=p.get_data_dir)\n",
    "get_media_dir(": "from nanobot.config.paths import get_media_dir\nm = get_media_dir()\n",
    "get_legacy_sessions_dir(": "import nanobot.config.paths as p\nd = p.get_legacy_sessions_dir()\n",
    "get_runtime_subdir(": "from nanobot.config import paths\nd = paths.get_runtime_subdir('x')\n",
    "Path.home()": "from pathlib import Path\nh = Path.home()\n",
    "pathlib.Path.home()": "import pathlib\nh = pathlib.Path.home()\n",
    "Path alias home()": "from pathlib import Path as P\nh = P.home()\n",
    "os.path.expanduser('~')": "import os\nh = os.path.expanduser('~')\n",
    "os.path.expanduser('~/x')": "import os\nh = os.path.expanduser('~/.nanobot')\n",
    "osp.expanduser('~')": "import os.path as osp\nh = osp.expanduser('~')\n",
    "from os import path": "from os import path\nh = path.expanduser('~')\n",
    "from os.path import expanduser": "from os.path import expanduser as eu\nh = eu('~')\n",
    "expanduser() no argument": "h = expanduser()\n",
    "Path('~').expanduser()": "from pathlib import Path\nh = Path('~').expanduser()\n",
    "os.path.expandvars": "import os\nx = os.path.expandvars('$HOME/x')\n",
    "from os.path import expandvars": "from os.path import expandvars\nx = expandvars(s)\n",
    "from os import *": "from os import *\n",
}

ALLOWED_SNIPPETS = {
    "Path(x).expanduser()": "from pathlib import Path\np = Path(x).expanduser()\n",
    "Path(x).expanduser().resolve()": "from pathlib import Path\np = Path(x).expanduser().resolve()\n",
    "os.path.expanduser(var)": "import os\np = os.path.expanduser(user_path)\n",
    "docstring mention": (
        'def f():\n    """Uses os.environ, Path.home(), get_data_dir() and load_config()."""\n'
    ),
    "comment mention": "# os.environ['HOME'] and os.getenv('X') and Path.home()\nx = 1\n",
    "string literal": "msg = 'set os.environ or call load_config()'\n",
    "unrelated environ attr": "x = request.environ_name\n",
    "env mapping get": "x = env.get('A')\n",
    "os.path.join": "import os\np = os.path.join(a, b)\n",
    "other .home attr": "x = user.home\n",
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_SNIPPETS))
def test_scanner_flags_forbidden(name: str) -> None:
    violations = scan_source(FORBIDDEN_SNIPPETS[name], "pkg/mod.py")
    assert violations, f"scanner missed forbidden pattern: {name}"
    assert all(str(v).startswith("pkg/mod.py:") for v in violations)
    assert all(v.line >= 1 for v in violations)


@pytest.mark.parametrize("name", sorted(ALLOWED_SNIPPETS))
def test_scanner_allows(name: str) -> None:
    assert scan_source(ALLOWED_SNIPPETS[name], "pkg/mod.py") == []


def test_scanner_reports_file_and_line() -> None:
    src = "import os\n\n\nx = os.getenv('A')\n"
    (violation,) = scan_source(src, "nanobot/example.py")
    assert str(violation).startswith("nanobot/example.py:4: ")


def test_allowlist_matching() -> None:
    assert _allowed("nanobot/cli/commands.py")
    assert _allowed("nanobot/config/schema.py")
    assert _allowed("nanobot/kernel/legacy.py")
    assert not _allowed("nanobot/kernel/env.py")
    assert not _allowed("nanobot/client/x.py")  # prefix must match a directory
