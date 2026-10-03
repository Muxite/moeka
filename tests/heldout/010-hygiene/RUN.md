# Running the 010-hygiene held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # e.g. /home/muk/projects/moeka-impl-010
COPY=$(mktemp -d /tmp/moeka010-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout, aiohttp
```

The copy must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout). Do not
install the `vec` extra: the embedding tests use an injected fake embedder and need neither
sqlite-vec nor numpy. `aiohttp` (in the `dev` extra) is needed by the `serve` tests.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/010-hygiene
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka010-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider --tb=no
```

- Whole suite: about 25-35 s, 203 tests. Every test has a hard timeout (pytest-timeout, signal
  method, 120 s).
- One part only: `pytest "$HT/test_c_pending.py" ...` (a = lint codes + SC-001, b = lint modes and
  the lint CLI, c = pending skills, d = metering, e = host tools guard, f = docs).
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- For your own triage only (never forward): add `--tb=short -rf`.
- pytest may print `PytestWarning: (rm_rf) error removing /tmp/pytest-of-<user>/garbage-*`; that is
  pytest pruning old basetemp dirs from other runs and is harmless.

## 3. Safety properties

- Offline: Dream uses a scripted `MagicMock` provider (as the repo's own Dream tests do); image and
  transcription HTTP is answered in process by patching `httpx.AsyncClient.send`; the embedder is a
  fake; HTTP(S) proxies point at a dead local port; real API keys are removed from the environment and
  only obviously fake keys (`sk-...FAKE...`) are used.
- `HOME` is a fresh temp directory for the session and again for each test (and for each CLI
  subprocess); every workspace, config file, usage DB and vec DB lives in pytest's per-test
  `tmp_path` or that HOME.
- Nothing is written to the live checkout, `~/.nanobot`, the implementer worktree or this directory
  (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`). `fixtures/live_skills/` are read-only copies
  of four live skills; the suite never reads `~/.nanobot` itself.
- Subprocesses: `python -m nanobot capabilities list|lint|pending|accept|reject` with
  `PYTHONPATH=$COPY`.
- In process: the host tools tests call the gateway runtime (`_run_gateway`) and the `serve` /
  `agent` CLI commands through `typer.testing.CliRunner`, with `ToolLoader.load` wrapped to drop named
  tools and a sentinel (`ChannelManager`, `create_app`, `AgentLoop.process_direct`) that stops startup
  before any server, channel or model call. The gateway's health server and WebUI channel are
  disabled; no port is bound.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-002   total=126 passed=124 failed=  2 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines
with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that carries several
ids counts under each of them. Never forward test names, assertion text, tracebacks or file paths
from this directory. `UNMAPPED` in the summary means a test lost its marker (fix the test).

## 5. Baseline

See the "Baseline" table in SPEC-MAP.md: on the spec commit `1163df0e`, 186 of 203 fail, 17 pass,
0 errors; all 8 ids have at least one failing test.
