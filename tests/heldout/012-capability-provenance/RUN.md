# Running the 012-capability-provenance held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # e.g. /home/muk/projects/moeka-impl-012
COPY=$(mktemp -d /tmp/moeka012-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout, jsonschema
```

The copy must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout). The
suite needs `cryptography` (already in `uv.lock`) and `jsonschema` (dev extra, used by
`schemas/validate.py`). The copy must contain `tests/core/test_import_boundary.py` (git archive
includes it) for the NFR-003 guard test; it skips otherwise.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/012-capability-provenance
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka012-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider --tb=no -rs
```

- Whole suite: about 35-50 s, 407 tests (kernel-level tests build one kernel each, ~0.9 s).
  Every test has a hard timeout (pytest-timeout, signal method, 60 s; the repo import-boundary
  guard subprocess 600 s).
- `-rs` prints skip reasons. On jifan today exactly one test skips:
  `SC-003 skipped: bwrap present but unusable here: bwrap: loopback: Failed RTM_NEWADDR: Operation
  not permitted` (unprivileged user namespaces are restricted by AppArmor). Where bwrap is not
  installed the reason reads `bwrap is absent`. Record the reason with the result.
- One part only: `pytest "$HT/test_c_exec.py" ...` (a1 cert/hash = `test_a_cert`, a2 verify,
  a3 trust, b1 config, b2 gate, b3 index, b4 protected, c1 manifest, c2 exec, d cli,
  e = SC-002, x non-functional).
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- For your own triage only (never forward): add `--tb=short -rf`.

## 3. Safety properties

- Offline: models are `moeka.testing.FakeProvider` only; HTTP(S) proxies point at a dead local
  port; no API keys are set (they are removed from the environment). The only sockets are a
  `127.0.0.1` listener the SC-003 tests open themselves.
- Keys are FAKE: deterministic Ed25519 keys derived from `sha256("h012-fake-<label>")` inside the
  tests (`_h012.Signer`); no key material is read from or written to disk outside per-test temp dirs.
- `HOME` is a fresh temp directory for the session and again for each test (and for each CLI
  subprocess); every workspace, trust dir, kernel state dir and SQLite index lives in pytest's
  per-test `tmp_path`.
- Nothing is written to the live checkout, `~/.nanobot`, the implementer worktree or this
  directory (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`).
- Subprocesses: `python -m nanobot capabilities ...` (FR-012, FR-006 list, FR-004 help), a
  `python -c` import probe (NFR-003), the repo's `tests/core/test_import_boundary.py` (NFR-003), a
  `bwrap --unshare-net ... /bin/true` probe, and the script tools under test (small `/bin/sh` and
  Python scripts written into the per-test workspace; the timeout test spawns a `sleep 300`
  grandchild and kills any survivor in `finally`).

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-010   total= 39 passed= 37 failed=  2 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines
with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`, plus the SC-003 skip
reason if it skipped. A test that carries several ids counts under each of them. Never forward
test names, assertion text, tracebacks or file paths from this directory. `UNMAPPED` in the
summary means a test lost its marker (fix the test).

## 5. Baseline

See the "Baseline" section of SPEC-MAP.md: on the spec commit `758fc05b`, 370 of 407 fail
(every one of the 19 requirement ids has at least one failing test), 36 pass, 1 skips.
