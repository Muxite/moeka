# Running the 008-heldout-sandbox held-out tests

Hidden from the implementer. Run from a throwaway copy of the implementer's tree; report back only
requirement ids and counts. **Never run through the runner of the tree under test** — it is the
code under test. Run it with the head's trusted interpreter (unsandboxed), the way 007 does.

These tests need **Docker** (24+, the host already has it). The tests themselves run unsandboxed by
the head; they drive `heldout_run.py run --sandbox docker ...` against toy trees and toy suites in
temp dirs. They never touch the real HOME, the real held-out root, `~/.nanobot` or the GPU.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree
COPY=$(mktemp -d /tmp/moeka008-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# or, to include uncommitted work:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
```

The copy must not be `/home/muk/projects/moeka` (the harness refuses the live checkout). Only
`pytest` (>=7) is needed in the interpreter you use; that same interpreter runs the trusted runner.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/008-heldout-sandbox
cd "$(mktemp -d)"
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka008-fr.json \
PYTHONPATH="$COPY" python3 -m pytest "$HT" -q -p no:cacheprovider
```

Via the runner itself (the 007 convention, head-trusted, unsandboxed; this is also the exact
command to drive the suite through `scripts/heldout_run.py`):

```bash
python3 "$COPY/scripts/heldout_run.py" run --profile generic \
  --repo-name moeka --feature 008-heldout-sandbox \
  --worktree "$COPY" --suite "$HT" --no-count --report-dir /tmp/moeka008-report
```

Knobs / markers:

- `-m "not slow"` skips the parity (SC-002) and perf (SC-006) and timeout-parity tests.
- `-m "not network"` skips the accepted-suite parity (it syncs moeka deps from PyPI).
- `-m docker` selects only the Docker-driven tests (most of the suite); `-m "not docker"` runs only
  the pure-CLI/API tests (usage errors, unchanged files, constants, docstring/help).
- `HELDOUT_008_REPO=<path>` names the tree under test explicitly (default: first `PYTHONPATH` entry
  holding `scripts/` with `nanobot/` or `scripts/heldout_run.py`).
- `HELDOUT_008_TMP=<dir>` moves the per-test temp bases (default `/tmp`).

The first sandboxed run builds the `heldout-sandbox:<sha12>` image (minutes); it is cached and the
suite never deletes it. Containers, networks and files a test creates are removed on teardown.

## 3. Safety

- Each test gets `/tmp/h8<token>-*` holding its own HOME, HELDOUT_ROOT (0700), TMPDIR, toy git repo
  and toy suite; the real held-out root and live checkout are never written.
- Teardown SIGKILLs every process whose argv/environ names the test's base, `docker rm -f -v`s the
  containers it created and `docker rmi`s any throwaway override image, then removes the base (using
  a root-in-container `rm` if a sandbox uid left files behind). The `heldout-sandbox:*` cache image
  is kept.
- Test-phase containers have `--network none`; the only allowed outbound traffic anywhere is the
  sync step's registry access on the default bridge.

## 4. Reporting to the implementer (redacted)

The run ends with `held-out requirement summary (ids and counts only)`, one line per id; with
`HELDOUT_REPORT=<file>` the same data is JSON. Forward only lines with `failed`/`error` > 0 as
`FR-xxx: <failed+error>/<total> failing`. Never forward test names, assertion text, tracebacks or
file paths. Round cap 4.

## 5. Baseline (spec-008 base a79edd11, no sandbox support), 2026-10-01

16 passed, 29 failed, 101 errors. The 16 passers are behaviour the sandbox leaves unchanged
(FR-020 unchanged files, `--sandbox <unknown>` usage errors, the FR-018 keywords already in the 007
text, and two non-sandbox behaviours). Per-id baseline counts are in SPEC-MAP.md.

## 6. On acceptance

Commit the suite to `tests/heldout/008-heldout-sandbox/` in moeka and run via
`scripts/run-heldout.sh 008-heldout-sandbox` (sets `PYTHONPATH=<copy>`, which the harness uses to
find the tree). Docker must be available on the host that runs it.
