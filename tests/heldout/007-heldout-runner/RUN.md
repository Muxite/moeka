# Running the 007-heldout-runner held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

**Bootstrap rule: run this suite BY HAND (this procedure, the 005 RUN.md pattern). Never run it
through `heldout-run` / `scripts/heldout_run.py` itself.** The runner is the code under test; it
lives in the implementer's tree and could misreport its own results (spec FR-005, tasks T020).

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # the implementer's moeka worktree
COPY=$(mktemp -d /tmp/moeka007-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
```

The copy must not be `/home/muk/projects/moeka` (the harness refuses the live checkout). No
`uv sync` is needed: the runner is stdlib only and the suite needs only `pytest` (any version
>= 7) in the interpreter you use, which is also the interpreter the runner's `generic` profile
hands to the inner canary suites.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/007-heldout-runner
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka007-fr.json \
PYTHONPATH="$COPY" python3 -m pytest "$HT" -q -p no:cacheprovider
```

(`uv run --project "$COPY" --extra dev pytest ...` works too after a `uv sync` of the copy.)

Useful knobs:

- Skip the slower tests (timeouts, signals, lock races): `-m "not slow"`.
- `HELDOUT_007_REPO=<path>` names the tree under test explicitly (default: the first `PYTHONPATH`
  entry holding `scripts/` and `nanobot/`).
- `HELDOUT_007_AWORK=<throwaway copy of awork-resume>` enables the FR-003 vendoring check
  (byte-identical files, `scripts/heldout_run.SOURCE`, awork `run-heldout.sh` unchanged).
- `HELDOUT_007_TMP=<dir>` moves the per-test temp bases (default `/tmp`).

## 3. Safety properties of the harness

- Every test gets `/tmp/h7<token>-*` holding its own `HOME`, `HELDOUT_ROOT` (mode 0700), `TMPDIR`,
  canary git repo and canary suite; the runner always gets `HOME`, `HELDOUT_ROOT`, `TMPDIR` pointing
  there. The real `~/projects/.heldout`, the real `HOME` and the live checkout are never written.
- Profiles `moeka` / `awork-resume` run only against a fake `uv` placed first on `PATH`; nothing
  syncs or downloads. Proxies point at a dead local port (offline).
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is set for the runner (inner pytest inherits it). When pytest
  comes from the user site, `PYTHONUSERBASE` is set so it stays importable under the temp HOME.
- Teardown SIGKILLs every process whose argv or environment names the test's temp base, then
  removes it.
- The suite never imports or runs anything from the tree under test except
  `scripts/heldout_run.py` (CLI and module) and `scripts/heldout-run`.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per id:

```
FR-029   total= 15 passed= 13 failed=  2 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines with
`failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that carries several ids
counts under each of them. Never forward test names, assertion text, tracebacks, canary tokens or
file paths from this directory. Round cap 4 (007-Q1 default: a round caused by a tester bug still
counts unless the head resets it, recording the reason).

How to map a failure by hand: every test carries `@pytest.mark.fr(...)`; SPEC-MAP.md lists the
groups, the untestable parts and the interpretations the tests rely on (check those first when
triaging a possible tester bug).

## 5. Baseline (consolidate/new-main c5340d24, before the feature)

`1 passed, 1 skipped, 251 errors` (2026-10-01): everything errors with "scripts/heldout_run.py is
missing" except FR-004's `run-heldout.sh` hash check (passes) and the opt-in awork test (skipped).

## 6. On acceptance

Commit the suite to `tests/heldout/007-heldout-runner/` in moeka and check
`scripts/run-heldout.sh 007-heldout-runner` (it sets `PYTHONPATH=<copy>`, which the harness uses
to find the tree).
