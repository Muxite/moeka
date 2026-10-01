#!/usr/bin/env python3
"""heldout-run: run a hidden (held-out) pytest suite against a throwaway copy of a tree.

The head agent runs this from its own checkout. It copies the tree under test (a git ref or a
working tree) into a scratch directory, places the suite inside the copy, runs pytest under a
wall-clock timeout in its own process group, maps every test to requirement ids (``fr`` markers,
``SPEC-MAP.json`` or the ``MAP`` literal of an ``fr_report.py``) and prints only redacted feedback:
ids with failing counts and totals. Full details go to a private report under the held-out root.

The same file is the pytest plugin loaded in the child with ``-p heldout_run``; it records
per-test results as JSON lines in ``$HELDOUT_RUN_RECORDS``.

Subcommands: ``run``, ``triage``, ``rounds``, ``check-isolation``; ``--version``.

STATED LIMIT (no adversarial isolation): this runner is not a sandbox. The suite runs in the
same process as the implementer's code, which can read the suite copy, write outside the copy,
or tamper with the recorded results, and the feedback counts are themselves a low-bandwidth
channel. The runner prevents accidental exposure (suite placement, redaction, cleanup), not a
hostile implementer. No OS sandbox is applied; wrap the runner (for example in bubblewrap or a
container) when a stronger boundary is needed.

Standard library only; it never imports moeka, nanobot or awr.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import warnings

VERSION = "1.0.0"

FEEDBACK_SCHEMA = "heldout-feedback.v1"
REPORT_SCHEMA = "heldout-report.v1"
ROUNDS_SCHEMA = "heldout-rounds.v1"
TRIAGE_SCHEMA = "heldout-triage.v1"

_ID_RX = re.compile(r"^(FR|NFR|SC)-[0-9]{3}[a-z]?$")


class _IdPattern(str):
    """The requirement-id regex source; also usable like a compiled pattern."""

    def match(self, string, *args):  # noqa: D102
        return _ID_RX.match(string, *args)

    def fullmatch(self, string, *args):  # noqa: D102
        return _ID_RX.fullmatch(string, *args)

    def search(self, string, *args):  # noqa: D102
        return _ID_RX.search(string, *args)

    @property
    def pattern(self) -> str:  # noqa: D102
        return str(self)


ID_PATTERN = _IdPattern(_ID_RX.pattern)
FEATURE_RX = re.compile(r"^[0-9]{3}-[a-z0-9][a-z0-9-]*$")
REPO_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

INVALID_ID = "INVALID_ID"
UNMAPPED = "UNMAPPED"

ERROR_CODES = frozenset(
    {
        "E_USAGE",
        "E_FEATURE",
        "E_SUITE_MISSING",
        "E_SUITE_IN_TREE",
        "E_SUITE_PERMS",
        "E_REPORT_IN_TREE",
        "E_RUNNER_IN_TREE",
        "E_MAP",
        "E_GIT",
        "E_SYNC",
        "E_BUSY",
        "E_ROUND_CAP",
    }
)
_EXIT_OF_CODE = {"E_SYNC": 3, "E_BUSY": 3, "E_ROUND_CAP": 4}

STATUSES = ("passed", "failed", "timeout", "collection_error", "infra_error")
COUNTED_STATUSES = frozenset({"passed", "failed", "timeout", "collection_error"})
_EXIT_OF_STATUS = {"passed": 0, "failed": 1, "timeout": 3, "collection_error": 3, "infra_error": 3}

DEFAULT_CAP = 4
DEFAULT_SYNC_TIMEOUT = 900.0
KILL_GRACE_S = 10.0
RECORDS_ENV = "HELDOUT_RUN_RECORDS"
ROUND_ENV = "HELDOUT_ROUND"

EXCLUDED_NAMES = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
)

# Profile defaults (FR-011). "{copy}" is replaced by the copy's path; "@scratch" means a new
# empty scratch directory.
PROFILES = {
    "moeka": {
        "sync": ["uv", "sync", "--project", "{copy}", "--extra", "dev", "-q"],
        "launcher": ["uv", "run", "--project", "{copy}", "--extra", "dev", "pytest"],
        "pythonpath": ["{copy}"],
        "cwd": "@scratch",
        "timeout": 5400.0,
    },
    "awork-resume": {
        "sync": None,
        "launcher": ["uv", "run", "--project", "{copy}", "--extra", "dev", "pytest"],
        "pythonpath": ["{copy}/src", "{copy}"],
        "cwd": "{copy}",
        "timeout": 1800.0,
    },
    "generic": {
        "sync": None,
        "launcher": ["@python", "-m", "pytest"],
        "pythonpath": ["{copy}"],
        "cwd": "@scratch",
        "timeout": 600.0,
    },
}

LIMIT_TEXT = (
    "Stated limit: heldout-run is not a sandbox and claims no adversarial isolation. The suite\n"
    "runs in the same process as the implementer's code, which can read the suite copy, write\n"
    "outside the copy or tamper with results; the feedback counts are a low-bandwidth channel.\n"
    "It prevents accidental exposure (placement, redaction, cleanup), not a hostile implementer.\n"
    "No OS sandbox is applied; wrap the runner when a stronger boundary is needed."
)


class HeldoutError(Exception):
    """A refusal or infra failure carrying one of ERROR_CODES."""

    def __init__(self, code: str):
        assert code in ERROR_CODES, code
        super().__init__(code)
        self.code = code

    @property
    def exit_code(self) -> int:
        return _EXIT_OF_CODE.get(self.code, 2)


class _Interrupted(BaseException):
    pass


# ---------------------------------------------------------------------------------------------
# id mapping and feedback (pure helpers)


def _is_id(value) -> bool:
    return isinstance(value, str) and _ID_RX.match(value) is not None


def _normalise_ids(value) -> tuple[str, ...]:
    # Documented shape: a list of id strings (a tuple is accepted from a .py literal).
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(dict.fromkeys(value))
    raise HeldoutError("E_MAP")


def _validate_map(obj) -> dict[str, tuple[str, ...]]:
    if not isinstance(obj, dict):
        raise HeldoutError("E_MAP")
    out: dict[str, tuple[str, ...]] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise HeldoutError("E_MAP")
        out[key] = _normalise_ids(value)
    return out


def _map_from_py(text: str):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        raise HeldoutError("E_MAP") from None
    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "MAP" for t in stmt.targets):
                node = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "MAP" and stmt.value:
                node = stmt.value
    if node is None:
        raise HeldoutError("E_MAP")
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise HeldoutError("E_MAP") from None


def load_id_map(path) -> dict[str, tuple[str, ...]]:
    """Read an id map: JSON ``{key: [ids]}`` (optionally under ``"map"``) or a ``.py`` MAP literal.

    A ``.py`` file is parsed, never imported or executed. Errors raise ``HeldoutError("E_MAP")``.
    """
    path = os.fspath(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise HeldoutError("E_MAP") from None
    if path.endswith(".py"):
        obj = _map_from_py(text)
    else:
        try:
            obj = json.loads(text)
        except ValueError:
            raise HeldoutError("E_MAP") from None
        if isinstance(obj, dict) and "map" in obj:
            # Wrapped form {"map": {...}}: the wrapper holds only the map, and it must be an object.
            if set(obj) != {"map"} or not isinstance(obj["map"], dict):
                raise HeldoutError("E_MAP")
            obj = obj["map"]
    return _validate_map(obj)


def _labels(ids) -> set[str]:
    if isinstance(ids, str):
        ids = (ids,)
    labels: set[str] = set()
    for value in ids or ():
        labels.add(value if _is_id(value) else INVALID_ID)
    return labels or {UNMAPPED}


_PREFIX_ORDER = {"FR": 0, "NFR": 1, "SC": 2, INVALID_ID: 3, UNMAPPED: 4}


def _id_sort_key(label: str):
    m = re.match(r"^(FR|NFR|SC)-([0-9]{3})([a-z]?)$", label)
    if m:
        return (_PREFIX_ORDER[m.group(1)], int(m.group(2)), m.group(3))
    return (_PREFIX_ORDER.get(label, 5), 0, label)


def _safe_label(label) -> bool:
    return _is_id(label) or label in (INVALID_ID, UNMAPPED)


def summarize(records, *, feature, round, cap, status) -> dict:  # noqa: A002
    """Build the ``heldout-feedback.v1`` object from test records (ids and counts only)."""
    per: dict[str, list[int]] = {}
    tests = failing = skipped = 0
    for rec in records:
        outcome = rec.get("outcome")
        if outcome == "skipped":
            skipped += 1
            continue
        bad = outcome != "passed"
        tests += 1
        failing += int(bad)
        for label in _labels(rec.get("ids")):
            entry = per.setdefault(label, [0, 0])
            entry[1] += 1
            entry[0] += int(bad)
    ordered = sorted(per, key=_id_sort_key)
    failing_obj = {k: {"failing": per[k][0], "total": per[k][1]} for k in ordered if per[k][0] > 0}
    return {
        "schema": FEEDBACK_SCHEMA,
        "feature": feature,
        "round": round,
        "round_cap": cap,
        "status": status,
        "cap_reached": bool(round is not None and round >= cap and status != "passed"),
        "failing": failing_obj,
        "totals": {
            "tests": tests,
            "failing": failing,
            "skipped": skipped,
            "ids": sum(1 for k in per if per[k][1] > 0),
            "ids_failing": len(failing_obj),
        },
    }


def format_feedback(feedback: dict) -> str:
    """Render the FR-021 text feedback (no trailing newline)."""
    rnd = feedback.get("round")
    r = "-" if rnd is None else str(int(rnd))
    status = feedback.get("status")
    if status not in STATUSES:
        status = "infra_error"
    feature = feedback.get("feature")
    if not isinstance(feature, str) or not FEATURE_RX.match(feature):
        feature = "-"
    cap = int(feedback.get("round_cap", DEFAULT_CAP))
    lines = [f"heldout-run: {feature} round {r}/{cap}: {status}"]
    failing = feedback.get("failing") or {}
    for label in sorted((k for k in failing if _safe_label(k)), key=_id_sort_key):
        v = failing[label]
        lines.append(f"{label}: {int(v['failing'])}/{int(v['total'])} failing")
    t = feedback.get("totals") or {}
    lines.append(
        f"total: {int(t.get('failing', 0))}/{int(t.get('tests', 0))} tests failing, "
        f"{int(t.get('skipped', 0))} skipped; {int(t.get('ids_failing', 0))}/{int(t.get('ids', 0))}"
        " ids failing"
    )
    if feedback.get("cap_reached"):
        lines.append("round cap reached: escalate to owner")
    return "\n".join(lines)


def _sanitize_feedback(fb: dict) -> dict:
    """Final redaction guard: only ids/labels, the validated feature and integers survive."""
    clean = dict(fb)
    clean["failing"] = {
        k: {"failing": int(v["failing"]), "total": int(v["total"])}
        for k, v in fb["failing"].items()
        if _safe_label(k)
    }
    if not (isinstance(fb["feature"], str) and FEATURE_RX.match(fb["feature"])):
        clean["feature"] = "-"
    return clean


# ---------------------------------------------------------------------------------------------
# requirement text (triage)

_LIST_ITEM_RX = re.compile(r"^([ \t]*)(?:[-*+]|[0-9]+[.)])(?:[ \t]|$)")
_HEADING_RX = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]|$)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def extract_requirement(spec_text: str, req_id: str) -> str | None:
    """Return the markdown list item starting ``- **<id>**`` (FR-037), or None when absent."""
    start_rx = re.compile(
        r"^([ \t]*)[-*+][ \t]+\*\*" + re.escape(req_id) + r"(?:[ \t]*\([^\n]*?\))?\*\*"
    )
    lines = spec_text.splitlines()
    for i, line in enumerate(lines):
        m = start_rx.match(line)
        if not m:
            continue
        base = len(m.group(1))
        out = [line]
        j = i + 1
        while j < len(lines):
            cur = lines[j]
            if not cur.strip():
                nxt = j + 1
                while nxt < len(lines) and not lines[nxt].strip():
                    nxt += 1
                if nxt >= len(lines) or _indent(lines[nxt]) == 0:
                    break
                out.append(cur)
                j += 1
                continue
            if _HEADING_RX.match(cur):
                break
            item = _LIST_ITEM_RX.match(cur)
            if item and len(item.group(1)) <= base:
                break
            out.append(cur)
            j += 1
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(line.rstrip() for line in out)
    return None


# ---------------------------------------------------------------------------------------------
# filesystem helpers


def _runner_file() -> str:
    return os.path.realpath(__file__)


def _runner_bytes() -> bytes:
    with open(_runner_file(), "rb") as fh:
        return fh.read()


def _runner_sha256() -> str:
    return hashlib.sha256(_runner_bytes()).hexdigest()


def _real(path) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _inside(path: str, base: str) -> bool:
    path, base = _real(path), _real(base)
    if path == base:
        return True
    base = base.rstrip(os.sep) + os.sep
    return path.startswith(base)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mkdir_private(path: str) -> None:
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        os.chmod(d, 0o700)


def _write_private(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def _write_json_atomic(path: str, obj) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    _write_private(tmp, json.dumps(obj, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def _rmtree(path: str | None) -> None:
    if not path or not os.path.lexists(path):
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _copy_filtered(src: str, dest: str) -> None:
    """Copy a directory tree as is: symlinks stay symlinks, excluded names are skipped."""
    os.makedirs(dest, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = dest if rel == "." else os.path.join(dest, rel)
        keep = []
        for d in dirnames:
            if d in EXCLUDED_NAMES:
                continue
            s = os.path.join(dirpath, d)
            if os.path.islink(s):
                os.symlink(os.readlink(s), os.path.join(target, d))
                continue
            os.mkdir(os.path.join(target, d))
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f in EXCLUDED_NAMES or f.endswith(".pyc"):
                continue
            s = os.path.join(dirpath, f)
            t = os.path.join(target, f)
            if os.path.islink(s):
                os.symlink(os.readlink(s), t)
            elif os.path.isfile(s):
                shutil.copy2(s, t)


def _tree_sha256(root: str) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_NAMES)
        rel_dir = os.path.relpath(dirpath, root)
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in sorted(filenames + links):
            if name in EXCLUDED_NAMES or name.endswith(".pyc"):
                continue
            p = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(p):
                h.update(b"L" + os.readlink(p).encode("utf-8", "surrogateescape") + b"\0")
            elif os.path.isfile(p):
                h.update(b"F")
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
                h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# git


def _git(repo: str, *args: str, timeout: float = 120.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise HeldoutError("E_GIT") from None
    if proc.returncode != 0:
        raise HeldoutError("E_GIT")
    return proc.stdout.decode("utf-8", "surrogateescape")


def _worktrees(repo: str, *, strict: bool) -> list[str]:
    try:
        out = _git(repo, "worktree", "list", "--porcelain")
    except HeldoutError:
        if strict:
            raise
        return []
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def _resolve_commit(repo: str, ref: str) -> str:
    if ref.startswith("-"):
        raise HeldoutError("E_GIT")
    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise HeldoutError("E_GIT")
    return commit


def _extract_archive(repo: str, commit: str, dest: str) -> None:
    try:
        proc = subprocess.Popen(
            ["git", "-C", repo, "archive", "--format=tar", commit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise HeldoutError("E_GIT") from None
    ok = True
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for member in tf:
                parts = member.name.split("/")
                if ".git" in parts or member.name.startswith("/") or ".." in parts:
                    continue
                if hasattr(tarfile, "tar_filter"):
                    tf.extract(member, dest, filter="tar")
                else:  # pragma: no cover - Python < 3.11.4
                    tf.extract(member, dest)
    except (tarfile.TarError, OSError):
        ok = False
    finally:
        if proc.stdout:
            proc.stdout.close()
        rc = proc.wait()
    if rc != 0 or not ok:
        raise HeldoutError("E_GIT")


# ---------------------------------------------------------------------------------------------
# argument parsing


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D102 - never echo argument values
        raise HeldoutError("E_USAGE")

    def exit(self, status=0, message=None):  # noqa: D102
        if status:
            raise HeldoutError("E_USAGE")
        raise SystemExit(0)


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise ValueError
    return value


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError
    return value


def _build_parser() -> _Parser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = _Parser(
        prog="heldout-run",
        description="Run a held-out pytest suite on a throwaway copy; print redacted feedback.\n\n"
        + LIMIT_TEXT,
        formatter_class=fmt,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="store_true", help="print version and sha256 of this file")
    sub = p.add_subparsers(dest="command", parser_class=_Parser)

    def common(sp):
        sp.add_argument("--repo-name", required=True)
        sp.add_argument("--feature", required=True)
        sp.add_argument("--heldout-root", help="held-out root (default: $HELDOUT_ROOT, else HOME)")

    r = sub.add_parser(
        "run",
        help="run a suite and print feedback",
        description="Run a held-out suite against a copy of a tree.\n\n" + LIMIT_TEXT,
        formatter_class=fmt,
        allow_abbrev=False,
    )
    common(r)
    r.add_argument("--worktree")
    r.add_argument("--repo")
    r.add_argument("--ref")
    r.add_argument("--profile", choices=sorted(PROFILES), default="generic")
    r.add_argument("--suite")
    r.add_argument("--suite-dest")
    r.add_argument("--report-dir")
    r.add_argument("--scratch")
    r.add_argument("--map", dest="map_file")
    r.add_argument("--timeout", type=_positive_float)
    r.add_argument("--sync-timeout", type=_positive_float, default=DEFAULT_SYNC_TIMEOUT)
    r.add_argument("--no-sync", action="store_true")
    r.add_argument("--pytest-arg", action="append", default=[])
    r.add_argument("--unset", action="append", default=[])
    r.add_argument("--forbid-under", action="append", default=[])
    r.add_argument("--cap", type=_positive_int, default=DEFAULT_CAP)
    r.add_argument("--no-count", action="store_true")
    r.add_argument("--keep", action="store_true")
    r.add_argument("--json", action="store_true")
    r.add_argument("--feedback-file")

    t = sub.add_parser(
        "triage",
        help="PRIVATE: failing assertions next to spec text (head only)",
        allow_abbrev=False,
    )
    common(t)
    t.add_argument("--round", type=_positive_int)
    t.add_argument("--id", dest="ids", action="append", default=[])
    t.add_argument("--spec", required=True)
    t.add_argument("--json", action="store_true")

    rd = sub.add_parser("rounds", help="list or reset rounds", allow_abbrev=False)
    common(rd)
    rd.add_argument("--reset", action="store_true")
    rd.add_argument("--reason")

    c = sub.add_parser(
        "check-isolation", help="placement and permission checks only", allow_abbrev=False
    )
    c.add_argument("--suite", required=True)
    c.add_argument("--repo")
    c.add_argument("--forbid-under", action="append", default=[])
    c.add_argument("--heldout-root")
    return p


def _preprocess(argv: list[str]) -> list[str]:
    """Let ``--pytest-arg -x`` work: glue a dash-leading value to its option."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--pytest-arg" and i + 1 < len(argv):
            out.append(f"--pytest-arg={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def _check_names(repo_name: str, feature: str) -> None:
    if not (isinstance(feature, str) and FEATURE_RX.match(feature)):
        raise HeldoutError("E_FEATURE")
    if not (isinstance(repo_name, str) and REPO_RX.match(repo_name)):
        raise HeldoutError("E_FEATURE")


def _heldout_root(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("HELDOUT_ROOT")
    if env:
        return os.path.abspath(env)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(os.path.abspath(home), "projects", ".heldout")


def _report_root(root: str, repo_name: str, feature: str) -> str:
    return os.path.join(root, "_reports", repo_name, feature)


def _check_perms(suite: str, root: str) -> None:
    for path in (suite, root):
        if os.path.isdir(path) and os.stat(path).st_mode & 0o077:
            raise HeldoutError("E_SUITE_PERMS")


def _forbidden_bases(tree: str | None, repo: str | None, forbid, *, strict_git: bool) -> list[str]:
    bases = []
    if tree:
        bases.append(tree)
    if repo:
        bases.extend(_worktrees(repo, strict=strict_git))
    bases.extend(os.path.abspath(p) for p in forbid)
    return bases


def _check_placement(bases, *, suite: str, report_dir: str | None, root: str) -> None:
    for base in bases:
        if _inside(suite, base):
            raise HeldoutError("E_SUITE_IN_TREE")
    for base in bases:
        if (report_dir and _inside(report_dir, base)) or _inside(root, base):
            raise HeldoutError("E_REPORT_IN_TREE")
    if report_dir and _inside(report_dir, suite):
        raise HeldoutError("E_REPORT_IN_TREE")


# ---------------------------------------------------------------------------------------------
# process control


class _State:
    def __init__(self):
        self.proc: subprocess.Popen | None = None


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_group(cmd, *, cwd, env, log_fh, timeout, state: _State) -> tuple[int | None, bool]:
    """Run ``cmd`` in a new process group with a wall-clock timeout; (returncode, timed_out)."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state.proc = proc
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    finally:
        if proc.poll() is None:
            _killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        # Leftover members of the group (background children of the suite).
        _killpg(proc.pid, signal.SIGKILL)
        state.proc = None
    return proc.returncode, timed_out


def _expand(items, copy: str) -> list[str]:
    return [s.replace("{copy}", copy) for s in items]


# ---------------------------------------------------------------------------------------------
# records -> tests


def _read_records(path: str):
    collected: dict[str, dict] = {}
    order: list[str] = []
    reports: dict[str, list[dict]] = {}
    collect_errors = 0
    finished = None
    if not os.path.exists(path):
        return collected, order, reports, collect_errors, finished
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            nodeid = str(rec.get("nodeid", ""))
            if kind == "collected":
                if nodeid not in collected:
                    order.append(nodeid)
                collected[nodeid] = rec
            elif kind == "report":
                reports.setdefault(nodeid, []).append(rec)
                if nodeid not in collected and nodeid not in order:
                    order.append(nodeid)
            elif kind == "collect_error":
                collect_errors += 1
            elif kind == "finish":
                finished = rec.get("exitstatus")
    return collected, order, reports, collect_errors, finished


def _key_from_nodeid(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def _outcome(phases: list[dict], timed_out: bool) -> tuple[str, str, str]:
    failed = [p for p in phases if p.get("outcome") == "failed"]
    if failed:
        text = "\n".join(str(p.get("longrepr") or "") for p in failed)
        return "failed", str(failed[0].get("when")), text
    if not any(p.get("when") == "teardown" for p in phases):
        why = "timeout" if timed_out else "missing"
        return "failed", why, f"no final result ({why})"
    skipped = [p for p in phases if p.get("outcome") == "skipped"]
    if skipped:
        if any(p.get("wasxfail") for p in skipped):
            return "passed", str(skipped[0].get("when")), str(skipped[0].get("longrepr") or "")
        return "skipped", str(skipped[0].get("when")), str(skipped[0].get("longrepr") or "")
    return "passed", "call", ""


def _build_tests(records_path: str, id_map: dict, timed_out: bool):
    collected, order, reports, collect_errors, finished = _read_records(records_path)
    tests = []
    for nodeid in order:
        info = collected.get(nodeid, {})
        key = str(info.get("key") or _key_from_nodeid(nodeid))
        marks = [str(m) for m in info.get("marks") or []]
        ids = list(dict.fromkeys(marks + list(id_map.get(key, ()))))
        phases = reports.get(nodeid, [])
        outcome, when, longrepr = _outcome(phases, timed_out)
        tests.append(
            {
                "nodeid": nodeid,
                "key": key,
                "ids": ids,
                "outcome": outcome,
                "when": when,
                "duration_s": round(sum(float(p.get("duration") or 0) for p in phases), 6),
                "longrepr": longrepr,
            }
        )
    return tests, bool(reports), collect_errors, finished


def _decide_status(*, timed_out, rc, has_reports, collect_errors, tests) -> str:
    if timed_out:
        return "timeout"
    if collect_errors or rc in (2, 3, 4, 5) or not has_reports:
        return "collection_error"
    if rc == 0 and not any(t["outcome"] == "failed" for t in tests):
        return "passed"
    return "failed"


# ---------------------------------------------------------------------------------------------
# rounds


def _load_rounds(path: str, cap: int) -> dict:
    if not os.path.exists(path):
        return {"schema": ROUNDS_SCHEMA, "cap": cap, "rounds": [], "resets": []}
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        raise HeldoutError("E_USAGE") from None
    if not isinstance(obj, dict):
        raise HeldoutError("E_USAGE")
    obj.setdefault("schema", ROUNDS_SCHEMA)
    obj.setdefault("cap", cap)
    obj.setdefault("rounds", [])
    obj.setdefault("resets", [])
    return obj


class _Lock:
    def __init__(self, report_root: str):
        self.path = os.path.join(report_root, ".lock")
        self.fd = -1

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = -1
            raise HeldoutError("E_BUSY") from None
        return self

    def __exit__(self, *exc):
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = -1


def _run_stamp() -> str:
    return _utcnow().strftime("run-%Y%m%dT%H%M%SZ")


def _fresh_run_dir(report_root: str) -> str:
    while True:
        path = os.path.join(report_root, _run_stamp())
        if not os.path.lexists(path):
            return path
        time.sleep(0.2)


# ---------------------------------------------------------------------------------------------
# output


def _emit_feedback(fb: dict, *, as_json: bool, feedback_file: str | None, out) -> None:
    fb = _sanitize_feedback(fb)
    if as_json:
        out.write(json.dumps(fb) + "\n")
    else:
        out.write(format_feedback(fb) + "\n")
    out.flush()
    if feedback_file:
        try:
            with open(feedback_file, "w", encoding="utf-8") as fh:
                json.dump(fb, fh)
                fh.write("\n")
        except OSError:
            pass


def _infra_feedback(feature: str, cap: int) -> dict:
    return summarize([], feature=feature, round=None, cap=cap, status="infra_error")


# ---------------------------------------------------------------------------------------------
# run


def _cmd_run(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    if bool(a.worktree) == bool(a.repo) or bool(a.repo) != bool(a.ref):
        raise HeldoutError("E_USAGE")
    profile = PROFILES[a.profile]
    root = _heldout_root(a.heldout_root)
    suite = os.path.abspath(a.suite) if a.suite else os.path.join(root, a.repo_name, a.feature)
    if not os.path.isdir(suite):
        raise HeldoutError("E_SUITE_MISSING")

    if a.worktree:
        tree = os.path.abspath(a.worktree)
        if not os.path.isdir(tree):
            raise HeldoutError("E_USAGE")
        repo_for_wt = tree
    else:
        tree = os.path.abspath(a.repo)
        if not os.path.isdir(tree):
            raise HeldoutError("E_GIT")
        repo_for_wt = tree
    report_root = _report_root(root, a.repo_name, a.feature)
    explicit_report = os.path.abspath(a.report_dir) if a.report_dir else None
    bases = _forbidden_bases(tree, repo_for_wt, a.forbid_under, strict_git=bool(a.repo))
    _check_placement(bases, suite=suite, report_dir=explicit_report or report_root, root=root)
    _check_perms(suite, root)
    if a.worktree and _inside(_runner_file(), tree):
        raise HeldoutError("E_RUNNER_IN_TREE")

    scratch = os.path.abspath(a.scratch or os.environ.get("TMPDIR") or tempfile.gettempdir())
    if not os.path.isdir(scratch):
        raise HeldoutError("E_USAGE")
    for base in (tree, suite, root):
        if _inside(scratch, base):
            raise HeldoutError("E_USAGE")
    suite_rel = a.suite_dest or os.path.join("tests", "heldout", a.feature)
    norm = os.path.normpath(suite_rel)
    if os.path.isabs(suite_rel) or norm in (".", "") or norm.split(os.sep)[0] == "..":
        raise HeldoutError("E_USAGE")

    if a.map_file:
        id_map, map_source = load_id_map(a.map_file), "--map"
    elif os.path.isfile(os.path.join(suite, "SPEC-MAP.json")):
        id_map, map_source = load_id_map(os.path.join(suite, "SPEC-MAP.json")), "SPEC-MAP.json"
    elif os.path.isfile(os.path.join(suite, "fr_report.py")):
        id_map, map_source = load_id_map(os.path.join(suite, "fr_report.py")), "fr_report.py"
    else:
        id_map, map_source = {}, None

    commit = _resolve_commit(tree, a.ref) if a.repo else None

    _mkdir_private(report_root)
    try:
        lock = _Lock(report_root).__enter__()
    except HeldoutError:
        _emit_feedback(_infra_feedback(a.feature, a.cap), as_json=a.json,
                       feedback_file=a.feedback_file, out=out)
        raise
    try:
        return _run_locked(a, out, profile=profile, root=root, suite=suite, tree=tree,
                           commit=commit, report_root=report_root, explicit_report=explicit_report,
                           scratch=scratch, suite_rel=norm, id_map=id_map, map_source=map_source)
    finally:
        lock.__exit__(None, None, None)


def _run_locked(a, out, *, profile, root, suite, tree, commit, report_root, explicit_report,
                scratch, suite_rel, id_map, map_source) -> int:
    rounds_path = os.path.join(report_root, "rounds.json")
    counted = not a.no_count
    state_doc = _load_rounds(rounds_path, a.cap)
    if counted and len(state_doc["rounds"]) >= a.cap:
        raise HeldoutError("E_ROUND_CAP")
    round_n = len(state_doc["rounds"]) + 1 if counted else None

    if explicit_report:
        report_dir = explicit_report
    elif counted:
        report_dir = os.path.join(report_root, f"round-{round_n:02d}")
        if os.path.lexists(report_dir):
            stale = f"stale-{_run_stamp()[4:]}-round-{round_n:02d}"
            os.replace(report_dir, os.path.join(report_root, stale))
    else:
        report_dir = _fresh_run_dir(report_root)
    _mkdir_private(report_dir)

    started = _utcnow()
    t0 = time.monotonic()
    tree_info = (
        {"mode": "repo", "repo": tree, "ref": a.ref, "commit": commit}
        if a.repo
        else {"mode": "worktree", "path": tree}
    )
    meta = {
        "runner_version": VERSION,
        "runner_sha256": _runner_sha256(),
        "repo": a.repo_name,
        "feature": a.feature,
        "round": round_n,
        "round_cap": a.cap,
        "counted": counted,
        "profile": a.profile,
        "tree": tree_info,
        "commit": commit,
        "suite": suite,
        "suite_sha256": _tree_sha256(suite),
        "map_source": map_source,
        "pytest_command": None,
        "started_at": _iso(started),
        "duration_s": None,
        "status": None,
        "pytest_exit_code": None,
        "error": None,
    }
    tests: list[dict] = []
    status = "infra_error"
    error_code = None
    base = copy = None
    state = _State()
    old_handlers = {}

    def _on_signal(signum, frame):
        raise _Interrupted(signum)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old_handlers[sig] = signal.signal(sig, _on_signal)
            except ValueError:  # not the main thread
                pass
        base = tempfile.mkdtemp(prefix="heldout-run-", dir=scratch)
        copy = tempfile.mkdtemp(prefix="heldout-copy-", dir=scratch)
        plugin_dir = os.path.join(base, "plugin")
        work = os.path.join(base, "work")
        child_tmp = os.path.join(base, "tmp")
        for d in (plugin_dir, work, child_tmp):
            os.mkdir(d, 0o700)
        records = os.path.join(base, "records.jsonl")
        if a.keep:
            meta["kept_copy"] = copy

        try:
            if a.repo:
                _extract_archive(tree, commit, copy)
            else:
                _copy_filtered(tree, copy)
            _rmtree(os.path.join(copy, ".git"))
            dest = os.path.join(copy, suite_rel)
            if os.path.lexists(dest):
                if os.path.isdir(dest) and not os.path.islink(dest):
                    _rmtree(dest)
                else:
                    os.unlink(dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _copy_filtered(suite, dest)
            with open(os.path.join(plugin_dir, "heldout_run.py"), "wb") as fh:
                fh.write(_runner_bytes())
        except HeldoutError:
            raise
        except OSError:
            raise HeldoutError("E_SYNC") from None

        env = dict(os.environ)
        for name in a.unset:
            env.pop(name, None)
        inherited = env.get("PYTHONPATH")
        pp = _expand(profile["pythonpath"], copy) + [plugin_dir]
        pp += [inherited] if inherited else []
        env["PYTHONPATH"] = os.pathsep.join(pp)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env[RECORDS_ENV] = records
        env[ROUND_ENV] = "-" if round_n is None else str(round_n)
        env["TMPDIR"] = child_tmp
        cwd = work if profile["cwd"] == "@scratch" else profile["cwd"].replace("{copy}", copy)

        if profile["sync"] and not a.no_sync:
            sync_log = os.path.join(report_dir, "sync.log")
            with open(sync_log, "wb") as log:
                os.chmod(sync_log, 0o600)
                try:
                    rc, to = _run_group(_expand(profile["sync"], copy), cwd=work, env=env,
                                        log_fh=log, timeout=a.sync_timeout, state=state)
                except OSError:
                    rc, to = None, False
            if to or rc != 0:
                raise HeldoutError("E_SYNC")

        launcher = [
            sys.executable if s == "@python" else s for s in _expand(profile["launcher"], copy)
        ]
        junit = os.path.join(report_dir, "junit.xml")
        cmd = launcher + [
            dest, "-q", "-p", "no:cacheprovider", "-p", "heldout_run", f"--junitxml={junit}",
        ] + list(a.pytest_arg)
        meta["pytest_command"] = cmd
        timeout = a.timeout if a.timeout else profile["timeout"]
        pytest_log = os.path.join(report_dir, "pytest.log")
        with open(pytest_log, "wb") as log:
            os.chmod(pytest_log, 0o600)
            try:
                rc, timed_out = _run_group(cmd, cwd=cwd, env=env, log_fh=log, timeout=timeout,
                                           state=state)
            except OSError:
                raise HeldoutError("E_SYNC") from None
        meta["pytest_exit_code"] = rc
        tests, has_reports, collect_errors, _finished = _build_tests(records, id_map, timed_out)
        status = _decide_status(timed_out=timed_out, rc=rc, has_reports=has_reports,
                                collect_errors=collect_errors, tests=tests)
    except HeldoutError as exc:
        status, error_code = "infra_error", exc.code
    except _Interrupted:
        status, error_code = "infra_error", "E_SYNC"
        meta["error"] = "interrupted"
    except Exception as exc:  # noqa: BLE001 - any runner-side failure is infra_error
        status, error_code = "infra_error", "E_SYNC"
        meta["error"] = type(exc).__name__
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except ValueError:
                pass
        try:
            if state.proc is not None and state.proc.poll() is None:
                _killpg(state.proc.pid, signal.SIGKILL)
            _rmtree(base)
            if not a.keep:
                _rmtree(copy)
        finally:
            for sig, handler in old_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, TypeError):
                    pass

    use_round = round_n if status in COUNTED_STATUSES else None
    fb = summarize(tests, feature=a.feature, round=use_round, cap=a.cap, status=status)
    meta["status"] = status
    meta["duration_s"] = round(time.monotonic() - t0, 3)
    if error_code:
        meta["error_code"] = error_code
    if status not in COUNTED_STATUSES:
        meta["round"] = None

    try:
        if not os.path.exists(os.path.join(report_dir, "junit.xml")):
            _write_private(os.path.join(report_dir, "junit.xml"), "<testsuites />\n")
        else:
            os.chmod(os.path.join(report_dir, "junit.xml"), 0o600)
        if not os.path.exists(os.path.join(report_dir, "pytest.log")):
            _write_private(os.path.join(report_dir, "pytest.log"), "")
        _write_json_atomic(os.path.join(report_dir, "report.json"),
                           {"schema": REPORT_SCHEMA, "meta": meta, "tests": tests})
        _write_json_atomic(os.path.join(report_dir, "feedback.json"), _sanitize_feedback(fb))
        if counted and status in COUNTED_STATUSES:
            state_doc["cap"] = a.cap
            state_doc["rounds"].append(
                {
                    "n": round_n,
                    "started_at": meta["started_at"],
                    "status": status,
                    "failing_ids": list(fb["failing"]),
                    "tree": tree_info,
                }
            )
            _write_json_atomic(os.path.join(report_root, "rounds.json"), state_doc)
        elif counted and not explicit_report:
            os.replace(report_dir, _fresh_run_dir(report_root))
    except OSError:
        status = "infra_error"
        error_code = error_code or "E_SYNC"
        fb = summarize(tests, feature=a.feature, round=None, cap=a.cap, status=status)

    _emit_feedback(fb, as_json=a.json, feedback_file=a.feedback_file, out=out)
    if error_code:
        raise HeldoutError(error_code)
    return _EXIT_OF_STATUS[status]


# ---------------------------------------------------------------------------------------------
# rounds / triage / check-isolation


def _cmd_rounds(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    if bool(a.reset) != bool(a.reason) or (a.reason is not None and not a.reason.strip()):
        raise HeldoutError("E_USAGE")
    root = _heldout_root(a.heldout_root)
    report_root = _report_root(root, a.repo_name, a.feature)
    path = os.path.join(report_root, "rounds.json")
    if not a.reset:
        doc = _load_rounds(path, DEFAULT_CAP)
        for r in doc["rounds"]:
            ids = [i for i in r.get("failing_ids") or [] if _safe_label(i)]
            st = r.get("status") if r.get("status") in STATUSES else "infra_error"
            out.write(" ".join([f"round {int(r.get('n', 0))}: {st}", *ids]) + "\n")
        return 0
    _mkdir_private(report_root)
    with _Lock(report_root):
        doc = _load_rounds(path, DEFAULT_CAP)
        now = _utcnow()
        moved = doc["rounds"]
        doc["resets"].append({"at": _iso(now), "reason": a.reason, "rounds": moved})
        doc["rounds"] = []
        _write_json_atomic(path, doc)
        archive = os.path.join(report_root, now.strftime("reset-%Y%m%dT%H%M%S%fZ"))
        for name in sorted(os.listdir(report_root)):
            if re.fullmatch(r"round-[0-9]+", name):
                _mkdir_private(archive)
                os.replace(os.path.join(report_root, name), os.path.join(archive, name))
    out.write(f"heldout-run: {a.feature} rounds reset ({len(moved)} moved)\n")
    return 0


def _assertion_text(longrepr: str) -> str:
    lines = (longrepr or "").splitlines()
    e_lines = [ln for ln in lines if ln.startswith("E ")]
    text = "\n".join(e_lines if e_lines else lines[-20:])
    return text[:4000]


def _latest_report_dir(report_root: str) -> str | None:
    best = None
    if not os.path.isdir(report_root):
        return None
    for name in os.listdir(report_root):
        if not re.fullmatch(r"round-[0-9]+|run-[0-9]{8}T[0-9]{6}Z", name):
            continue
        path = os.path.join(report_root, name)
        rj = os.path.join(path, "report.json")
        if not os.path.isfile(rj):
            continue
        try:
            with open(rj, encoding="utf-8") as fh:
                started = json.load(fh)["meta"]["started_at"] or ""
        except (OSError, ValueError, KeyError, TypeError):
            started = ""
        key = (started, os.stat(rj).st_mtime)
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None


def _cmd_triage(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    root = _heldout_root(a.heldout_root)
    report_root = _report_root(root, a.repo_name, a.feature)
    if a.round:
        report_dir = os.path.join(report_root, f"round-{a.round:02d}")
    else:
        report_dir = _latest_report_dir(report_root)
    try:
        with open(os.path.join(report_dir or "", "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        with open(a.spec, encoding="utf-8") as fh:
            spec_text = fh.read()
    except (OSError, ValueError):
        raise HeldoutError("E_USAGE") from None
    wanted = list(dict.fromkeys(a.ids))
    entries = []
    for t in report.get("tests", []):
        if t.get("outcome") != "failed":
            continue
        ids = [str(i) for i in t.get("ids") or []]
        tags = ids + sorted(_labels(ids) - set(ids))
        if wanted:
            hits = [w for w in wanted if w in tags]
            if not hits:
                continue
            head = hits[0]
        else:
            head = tags[0]
        spec_parts = []
        for i in ids or [UNMAPPED]:
            text = extract_requirement(spec_text, i) if _is_id(i) else None
            spec_parts.append(text if text is not None else "(not found in spec)")
        entries.append(
            {
                "id": head,
                "nodeid": t.get("nodeid"),
                "assertion": _assertion_text(t.get("longrepr") or ""),
                "spec": "\n".join(spec_parts),
            }
        )
    if a.json:
        # heldout-triage.v1: exactly one JSON object; no banner line in JSON mode.
        out.write(json.dumps({"schema": TRIAGE_SCHEMA, "entries": entries}, indent=2) + "\n")
        return 0
    lines = ["PRIVATE TRIAGE: not for the implementer"]
    for e in entries:
        lines.append(f"== {e['id']}: {e['nodeid']}")
        lines.append("assertion:")
        lines.append(e["assertion"])
        lines.append("spec:")
        lines.append(e["spec"])
    out.write("\n".join(lines) + "\n")
    return 0


def _cmd_check_isolation(a, out) -> int:
    root = _heldout_root(a.heldout_root)
    suite = os.path.abspath(a.suite)
    if not os.path.isdir(suite):
        raise HeldoutError("E_SUITE_MISSING")
    repo = os.path.abspath(a.repo) if a.repo else None
    bases = _forbidden_bases(repo, repo, a.forbid_under, strict_git=False)
    _check_placement(bases, suite=suite, report_dir=None, root=root)
    _check_perms(suite, root)
    out.write("heldout-run: isolated\n")
    return 0


# ---------------------------------------------------------------------------------------------
# entry point


def main(argv: list[str] | None = None) -> int:
    """The CLI. Returns the exit code; never raises for refusals."""
    out, err = sys.stdout, sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            try:
                args = _build_parser().parse_args(_preprocess(argv))
            except SystemExit as exc:  # --help
                return int(exc.code or 0)
            if args.version:
                if args.command:
                    raise HeldoutError("E_USAGE")
                out.write(f"heldout-run {VERSION} sha256:{_runner_sha256()}\n")
                return 0
            if args.command == "run":
                return _cmd_run(args, out)
            if args.command == "triage":
                return _cmd_triage(args, out)
            if args.command == "rounds":
                return _cmd_rounds(args, out)
            if args.command == "check-isolation":
                return _cmd_check_isolation(args, out)
            raise HeldoutError("E_USAGE")
        except HeldoutError as exc:
            err.write(f"heldout-run: error {exc.code}\n")
            err.flush()
            return exc.exit_code
        except KeyboardInterrupt:
            err.write("heldout-run: error E_SYNC\n")
            return 3
        except Exception:  # noqa: BLE001 - never print a traceback (it carries paths)
            err.write("heldout-run: error E_SYNC\n")
            return 3


# ---------------------------------------------------------------------------------------------
# pytest plugin (loaded in the child with ``-p heldout_run``); no pytest import needed


def _emit_record(rec: dict) -> None:
    path = os.environ.get(RECORDS_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def _flatten_marks(args) -> list[str]:
    out: list[str] = []
    for value in args:
        if isinstance(value, (list, tuple, set, frozenset)):
            out.extend(_flatten_marks(value))
        else:
            out.append(str(value))
    return out


def pytest_configure(config):
    config.addinivalue_line("markers", "fr(*ids): requirement ids this held-out test proves")


def pytest_collection_finish(session):
    if not os.environ.get(RECORDS_ENV):
        return
    for item in session.items:
        marks: list[str] = []
        for mark in item.iter_markers("fr"):
            marks.extend(_flatten_marks(mark.args))
        key = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        _emit_record({"type": "collected", "nodeid": item.nodeid, "key": key, "marks": marks})


def pytest_collectreport(report):
    if report.failed and os.environ.get(RECORDS_ENV):
        _emit_record(
            {
                "type": "collect_error",
                "nodeid": report.nodeid,
                "longrepr": str(report.longrepr)[:100000],
            }
        )


def pytest_runtest_logreport(report):
    if not os.environ.get(RECORDS_ENV):
        return
    _emit_record(
        {
            "type": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": hasattr(report, "wasxfail"),
            "duration": getattr(report, "duration", 0.0),
            "longrepr": str(report.longrepr)[:100000] if report.longrepr else "",
        }
    )


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get(RECORDS_ENV):
        _emit_record({"type": "finish", "exitstatus": int(exitstatus)})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
