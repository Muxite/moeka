# Running the 005-multi-instance held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # the implementer's branch checkout
COPY=$(mktemp -d /tmp/moeka005-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state; or rsync -a --exclude .git
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout, telegram
```

The copy must not contain `.git` (tests that write repo-level `.env`/`keys.env` skip otherwise)
and must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout).

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/005-multi-instance
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka005-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider
```

Useful subsets and knobs:

- Skip the long stress/repetition tests: add `-m "not slow"`.
- Skip containers: add `-m "not docker"` (docker tests also skip by themselves when no daemon
  answers; the image build is the slow part, about 5-15 minutes on first build).
- `HELDOUT_SC004_RUNS` (default 10) and `HELDOUT_SC004_PROCS` (default 8) size the SC-004
  budget stress test.
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- Every test has a hard timeout (pytest-timeout, signal method; default 120 s, longer where
  marked).

## 3. Safety properties of the harness

- `HOME` is a fresh `/tmp/mh*` directory per test (and `/tmp/mhs*` for the session).
- `XDG_RUNTIME_DIR` is removed and `DBUS_SESSION_BUS_ADDRESS` points nowhere, so a stray
  `systemctl --user` cannot reach the real user manager.
- Guard executables named `systemctl`, `loginctl`, `sudo`, `journalctl`, `nanobot`, `pkill`,
  `killall`, `pgrep`, `ps` come first on `PATH` for every shell-script run; they log and fail.
  The contract fakes are passed through `MOEKA_SYSTEMCTL`, `MOEKA_LOGINCTL`,
  `MOEKA_NANOBOT_BIN`.
- All HTTP(S) proxies point at a dead local port (offline); loopback is exempt.
- Teardown SIGKILLs every process whose argv or environment names the test's temp dirs, then
  removes them. Docker tests use unique names/tags/ports and `rm -f -v` / `down -v --rmi local`.
- The live checkout, `~/.nanobot`, `~/.config/systemd/user/moeka.service` and real tokens are
  never touched.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-004   total=  9 passed=  7 failed=  2 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only
lines with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that
carries several ids counts under each of them. Never forward test names, assertion text,
tracebacks or file paths from this directory.

How to map a failure by hand: every test carries `@pytest.mark.fr(...)`; SPEC-MAP.md lists
the tests per id. `UNMAPPED` in the summary means a test lost its marker (fix the test).

## 5. Baseline (consolidate/new-main before the feature)

See the "Baseline" section of SPEC-MAP.md for the numbers measured on 2026-10-01.
