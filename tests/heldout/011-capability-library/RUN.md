# Running the 011-capability-library held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # e.g. /home/muk/projects/moeka-impl-011
COPY=$(mktemp -d /tmp/moeka011-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout
```

The copy must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout). Do not
install the `vec` extra (the contract says the suite runs without it). The copy must contain
`tests/core/test_import_boundary.py` and `tests/kernel/test_no_ambient_reads.py` (git archive
includes them) for the NFR-004 guard tests; they skip otherwise.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/011-capability-library
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka011-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider --tb=no
```

- Whole default suite: about 60 s, 431 tests (the two 500-example property tests take ~25 s of
  that). Every test has a hard timeout (pytest-timeout, signal method, 60 s; property tests 900 s;
  the repo-guard and benchmark subprocess runs 400-560 s).
- The NFR-001 benchmark is marked `bench` and deselected by `pytest.ini` (`-m "not bench"`). Run it
  on jifan, idle, with `-m bench` (or `-m ""` for everything).
- One part only: `pytest "$HT/test_d_select.py" ...` (a1/a2 = `test_a_*`, b, c, d1 = select,
  d2 = prompt, e = library, x = non-functional).
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- For your own triage only (never forward): add `--tb=short -rf`.

## 3. Safety properties

- Offline: models are `moeka.testing.FakeProvider` only; HTTP(S) proxies point at a dead local
  port; no API keys are set (they are removed from the environment).
- `HOME` is a fresh temp directory for the session and again for each test (and for each CLI
  subprocess); every kernel state dir, work dir, skills tree and SQLite index lives in pytest's
  per-test `tmp_path`.
- Nothing is written to the live checkout, `~/.nanobot`, the implementer worktree or this
  directory (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`).
- Subprocesses: `python -m nanobot capabilities ...` (FR-013), small `python -c` probes for
  cross-process determinism (FR-005/FR-008) and imports (NFR-003/NFR-004), the repo's two guard
  test files (NFR-004) and the repo's benchmark test(s) under `tests/capabilities` that read
  `cases.jsonl` (NFR-002), all with `PYTHONPATH=$COPY`.
- The FR-005 purity test temporarily replaces `open`, `os.stat`, `Path.exists`, `socket.socket`
  etc. with raising stubs around one `extract` call and restores them in `finally`.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-007   total= 51 passed= 49 failed=  2 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines
with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that carries several
ids counts under each of them. Never forward test names, assertion text, tracebacks or file paths
from this directory. `UNMAPPED` in the summary means a test lost its marker (fix the test).

## 5. Baseline

See the "Baseline" section of SPEC-MAP.md: on the spec commit `f0533069`, 424 of 431 fail or
error (22 ids with at least one failing test).
