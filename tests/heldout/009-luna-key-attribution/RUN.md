# Running the 009-luna-key-attribution held-out tests

These tests are hidden from the implementer. Run them only from a throwaway copy of the
implementer's tree; report back only requirement ids and counts.

## 1. Make a throwaway copy

```bash
IMPL=/path/to/implementer/worktree          # the implementer's branch checkout
COPY=$(mktemp -d /tmp/moeka009-copy.XXXXXX)
git -C "$IMPL" archive HEAD | tar -x -C "$COPY"   # committed state
# include uncommitted work instead:  rsync -a --exclude .git --exclude .venv "$IMPL"/ "$COPY"/
uv sync --project "$COPY" --extra dev -q          # pytest, pytest-asyncio, pytest-timeout, jsonschema
```

The copy must not be `/home/muk/projects/moeka` (the conftest refuses the live checkout). The
NFR-002 guard needs the repo's own `tests/llm_usage/`, `tests/kernel/test_ledger.py`,
`tests/kernel/test_usage_surface.py`, `tests/schemas/test_schemas.py` and
`tests/providers/test_llm_usage_observer.py` (git archive includes them); it skips otherwise.

## 2. Run

```bash
HT=/home/muk/projects/.heldout/moeka/009-luna-key-attribution
cd "$(mktemp -d)"                                 # any scratch cwd
PYTHONDONTWRITEBYTECODE=1 HELDOUT_REPORT=/tmp/moeka009-fr.json \
PYTHONPATH="$COPY" uv run --project "$COPY" --extra dev \
  pytest "$HT" -q -p no:cacheprovider --tb=no
```

- Whole suite: about 40 s, 128 tests. Every test has a hard timeout (pytest-timeout, signal
  method, 60 s; CLI tests 120 s; the NFR-002 guard run 400 s).
- One requirement area only: `pytest "$HT/test_f4_query.py" ...` (f1 ... f8 = FR-001 ... FR-008,
  x = NFR-001/NFR-002).
- `MOEKA_HELDOUT_REPO=<path>` overrides the repo root (default: the directory holding the
  importable `nanobot` package, i.e. `$COPY`).
- For your own triage only (never forward): add `--tb=short -rf`.

## 3. Safety properties

- No real key is ever read or printed: the conftest removes `OPENROUTER_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `MOEKA_WORKSPACE`; every key in the suite is a fake
  literal (`sk-or-test-AAAA...`). Providers are OpenAI-compatible subclasses whose `chat` returns
  a scripted response, so nothing touches the network; HTTP(S) proxies point at a dead port.
- `HOME` is a fresh temp directory for the session and again for each test; every usage store
  lives in pytest's per-test `tmp_path`.
- Nothing is written to the live checkout, `~/.nanobot`, the implementer worktree or this
  directory (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`).
- Subprocesses: FR-008 runs `python -m nanobot usage keys --workspace <tmp instance>` with
  `MOEKA_WORKSPACE=<tmp instance>` and a temp `HOME`; NFR-002 runs the repo's own usage/ledger/
  schema test files from `$COPY`.

## 4. Reporting to the implementer (redacted)

The run ends with a section `held-out requirement summary (ids and counts only)`, one line per
id:

```
FR-004   total= 12 passed= 11 failed=  1 error=  0 skipped=  0
```

With `HELDOUT_REPORT=<file>` the same data is written as JSON. Send the implementer only lines
with `failed`/`error` > 0, as `FR-xxx: <failed+error>/<total> failing`. A test that carries several
ids counts under each of them. Never forward test names, assertion text, tracebacks or file paths
from this directory. `UNMAPPED` in the summary means a test lost its marker (fix the test).

## 5. Baseline

See the "Baseline" section of SPEC-MAP.md: on the spec commit `9920dafb` (pre-implementation),
115 of 128 fail, 0 errors, 0 collection errors (12 ids with at least one failing test).
