#!/usr/bin/env python3
"""Stable marks, owner-triggered revert and the boot guard (spec 015).

Stdlib only, on purpose: the boot guard must still run when moeka's own code is broken, so
this file imports nothing from the ``nanobot`` package. ``bin/moeka.sh run`` calls
``guard`` before it starts the gateway; the gateway calls ``healthy`` once it has stayed up.

Model
- ``mark`` tags the current commit ``moeka-stable/<UTC stamp>`` (annotated, refuses a dirty tree).
- ``revert`` checks a stable tag out (detached HEAD) and records where it came from in
  ``<state>/revert-state.json``; the caller then restarts the process so the old code loads.
  ``back`` returns to the recorded branch.
- ``guard`` counts boots that never reached ``healthy``. Three on the same commit within ten
  minutes, with a stable tag to go back to, reverts to the newest stable tag on its own.
  It never blocks startup and never raises.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STABLE_PREFIX = "moeka-stable/"
STATE_FILE = "revert-state.json"
GUARD_FILE = ".boot-guard.json"
GUARD_THRESHOLD = 3
GUARD_WINDOW_S = 600


class RevertError(Exception):
    """A refusal with a message that is safe to show the owner."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
    )
    if check and proc.returncode != 0:
        raise RevertError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def is_checkout(repo: Path) -> bool:
    return (Path(repo) / ".git").exists()


def head(repo: Path) -> tuple[str, str | None]:
    """(commit sha, branch name or ``None`` when detached)."""
    sha = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    return sha, branch


def dirty_files(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return [line[3:] for line in out.splitlines() if line.strip()]


def stable_tags(repo: Path) -> list[dict[str, str]]:
    """Stable tags, newest first."""
    out = _git(
        # Last --sort is the primary key: newest first, ties broken by the (timestamped) name.
        repo, "for-each-ref", "--sort=-refname", "--sort=-creatordate",
        "--format=%(refname:short)|%(objectname)|%(*objectname)|%(creatordate:iso8601)|%(contents:subject)",
        f"refs/tags/{STABLE_PREFIX}",
    )
    tags = []
    for line in out.splitlines():
        name, obj, peeled, when, note = (line.split("|", 4) + [""] * 5)[:5]
        tags.append({"tag": name, "sha": peeled or obj, "when": when, "note": note})
    return tags


def mark_stable(repo: Path, note: str = "") -> str:
    """Tag the current commit as stable. Refuses a dirty tree: a mark must be reproducible."""
    dirty = dirty_files(repo)
    if dirty:
        raise RevertError(
            "Refusing to mark a dirty tree as stable (commit first): " + ", ".join(dirty[:5]),
        )
    sha, _ = head(repo)
    existing = [t for t in stable_tags(repo) if t["sha"] == sha]
    if existing:
        return existing[0]["tag"]
    base = STABLE_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    names = {t["tag"] for t in stable_tags(repo)}
    tag, n = base, 1
    while tag in names:  # two marks inside one second
        n += 1
        tag = f"{base}-{n}"
    _git(repo, "tag", "-a", tag, "-m", (note or "marked stable by owner")[:200], sha)
    return tag


def resolve_target(repo: Path, target: str) -> dict[str, str]:
    tags = stable_tags(repo)
    if not tags:
        raise RevertError("No stable version is marked yet. Use /stable on a version you trust.")
    target = (target or "latest").strip()
    sha, _ = head(repo)
    if target == "latest":
        return tags[0]
    if target == "previous":
        for tag in tags:
            if tag["sha"] != sha:
                return tag
        raise RevertError("There is no earlier stable version than the one running.")
    matches = [t for t in tags if t["tag"] == target or t["tag"].endswith("/" + target)]
    if len(matches) == 1:
        return matches[0]
    raise RevertError(
        f"{target!r} is not a stable mark. Stable marks: "
        + ", ".join(t["tag"].removeprefix(STABLE_PREFIX) for t in tags[:5]),
    )


def _write_state(state_dir: Path, data: dict[str, object]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / STATE_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_state(state_dir: Path) -> dict[str, object] | None:
    try:
        return json.loads((Path(state_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def revert(repo: Path, state_dir: Path, target: str = "latest", *, force: bool = False) -> dict[str, str]:
    """Check *target* out. The caller restarts the process afterwards."""
    chosen = resolve_target(repo, target)
    from_sha, from_branch = head(repo)
    if chosen["sha"] == from_sha:
        raise RevertError(f"Already running {chosen['tag'].removeprefix(STABLE_PREFIX)}.")
    dirty = dirty_files(repo)
    stash = ""
    if dirty:
        if not force:
            raise RevertError(
                "The working tree has uncommitted changes (" + ", ".join(dirty[:5])
                + "). Say '/revert " + target + " force' to stash them and revert anyway.",
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stash = f"moeka-revert-{stamp}"
        _git(repo, "stash", "push", "-m", stash)
    _git(repo, "checkout", "--detach", chosen["tag"])
    previous = read_state(state_dir) or {}
    _write_state(state_dir, {
        # Keep the first origin across chained reverts so `back` still reaches the branch.
        "from_sha": previous.get("from_sha", from_sha),
        "from_branch": previous.get("from_branch", from_branch),
        "to_tag": chosen["tag"], "to_sha": chosen["sha"], "stash": stash,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return {**chosen, "stash": stash}


def back(repo: Path, state_dir: Path) -> dict[str, str]:
    state = read_state(state_dir)
    if not state:
        raise RevertError("Nothing to go back to: no revert is recorded.")
    branch = state.get("from_branch")
    ref = str(branch) if branch else str(state.get("from_sha"))
    if dirty_files(repo):
        raise RevertError("The working tree has uncommitted changes; resolve them first.")
    _git(repo, "checkout", ref)
    (Path(state_dir) / STATE_FILE).unlink(missing_ok=True)
    sha, _ = head(repo)
    return {"ref": ref, "sha": sha}


# -- boot guard --------------------------------------------------------------------------------


def _guard_read(state_dir: Path) -> dict[str, object]:
    try:
        return json.loads((Path(state_dir) / GUARD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guard_write(state_dir: Path, data: dict[str, object]) -> None:
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        (Path(state_dir) / GUARD_FILE).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def healthy(state_dir: Path) -> None:
    """The gateway stayed up: forget earlier failed boots."""
    _guard_write(state_dir, {"n": 0})


def guard(
    repo: Path, state_dir: Path, *, now: float | None = None,
    threshold: int = GUARD_THRESHOLD, window_s: int = GUARD_WINDOW_S,
) -> str:
    """Call before starting the gateway. Returns a one-line report (empty when idle)."""
    now = time.time() if now is None else now
    try:
        if not is_checkout(repo):
            return ""
        sha, _ = head(repo)
        data = _guard_read(state_dir)
        same = data.get("sha") == sha and now - float(data.get("since", 0)) <= window_s
        failed = int(data.get("n", 0)) if same else 0
        if failed >= threshold:
            tags = stable_tags(repo)
            if tags and tags[0]["sha"] != sha:
                result = revert(repo, state_dir, "latest", force=True)
                _guard_write(state_dir, {"n": 0})
                return (
                    f"boot guard: {failed} boots of {sha[:8]} never became healthy; reverted to "
                    f"{result['tag']}" + (f" (changes stashed as {result['stash']})" if result["stash"] else "")
                )
            _guard_write(state_dir, {"n": 0})
            return f"boot guard: {failed} unhealthy boots of {sha[:8]} and no earlier stable mark to go back to"
        _guard_write(state_dir, {
            "sha": sha, "n": failed + 1, "since": float(data.get("since", now)) if same else now,
        })
    except Exception as exc:  # never block startup
        return f"boot guard skipped: {exc}"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["guard", "healthy", "mark", "list", "revert", "back"])
    parser.add_argument("target", nargs="?", default="latest")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "guard":
            report = guard(args.repo, args.state)
            if report:
                print(report)
        elif args.action == "healthy":
            healthy(args.state)
        elif args.action == "mark":
            print(mark_stable(args.repo, "" if args.target == "latest" else args.target))
        elif args.action == "list":
            for tag in stable_tags(args.repo):
                print(tag["tag"], tag["sha"][:8], tag["when"])
        elif args.action == "revert":
            print(revert(args.repo, args.state, args.target, force=args.force))
        else:
            print(back(args.repo, args.state))
    except RevertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
