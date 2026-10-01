# Running the 006-rsi-kernel-prerequisites held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # the implementer's branch checkout
COPY=$(mktemp -d /tmp/moeka006-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout
```

The copy must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout). It
must contain `tests/core/test_import_boundary.py` and `tests/kernel/test_no_ambient_reads.py`
(git archive includes them) for the FR-042 tests; they skip otherwise.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/006-rsi-kernel-prerequisites
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka006-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider --tb=no
```

- Whole suite: about 30 s, 212 tests. Every test has a hard timeout (pytest-timeout, signal
  method, 60 s; the FR-042 guard run 400 s).
- One K item only: `pytest "$HT/test_k4_max_tool_errors.py" ...` (k1 ... k6, x = cross-cutting).
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- For your own triage only (never forward): add `--tb=short -rf`.

## 3. Safety properties

- Offline: models are `moeka.testing.FakeProvider` only; HTTP(S) proxies point at a dead local port.
- `HOME` is a fresh temp directory for the session and again for each test; every kernel's
  `state_dir` / `work_dir` / skills trees live in pytest's per-test `tmp_path`.
- Nothing is written to the live checkout, `~/.nanobot`, the implementer worktree or this
  directory (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`).
- The only subprocess is FR-042's run of the two repo guard test files from `$COPY`.
- The K6 exec test runs `cat skills/alpha/SKILL.md` inside the per-test work dir (or is refused
  by the sandbox); it asserts only that no `skill.read` is emitted.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-024   total=  5 passed=  4 failed=  1 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines
with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that carries several
ids counts under each of them. Never forward test names, assertion text, tracebacks or file paths
from this directory. `UNMAPPED` in the summary means a test lost its marker (fix the test).

## 5. Baseline

See the "Baseline" section of SPEC-MAP.md: on consolidate/new-main `c5340d24`, 185 of 212 fail
(45 ids with at least one failing test).
