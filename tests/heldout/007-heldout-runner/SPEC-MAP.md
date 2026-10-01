# SPEC-MAP: held-out tests for spec 007-heldout-runner

Source: `specs/007-heldout-runner/spec.md` (branch `spec/wave2`), Test Harness Contract section only.
253 collected tests from 241 test functions in 9 files, one per FR group. Every test carries
`@pytest.mark.fr(...)`; the conftest summary groups results by id (a test with several ids counts
under each).

| Group | File | FRs | Collected |
|-------|------|-----|-----------|
| A packaging and trust | `test_a_packaging.py` | FR-001..005, FR-039 | 13 |
| B tree copy, placement, cleanup | `test_b_copy.py` | FR-006..010, SC-003 | 23 |
| C profiles, invocation, timeout, sync, collection, lock | `test_c_execution.py` | FR-011..016, SC-004 | 27 |
| D id mapping and outcomes | `test_d_mapping.py` | FR-017..020, SC-001 | 29 |
| E feedback text/JSON, names, stderr, exit codes, Python API | `test_e_feedback.py` | FR-021..026 | 52 |
| F private report and isolation | `test_f_report_isolation.py` | FR-027..031, SC-003 | 37 |
| G rounds | `test_g_rounds.py` | FR-032..035, SC-006 | 19 |
| H triage | `test_h_triage.py` | FR-036..038, SC-007 | 16 |
| I redaction (canary suite) | `test_i_redaction.py` | SC-002, FR-024..026 (+ the FR of each refusal) | 37 |

Collected tests per id (a test counts under every id it carries): FR-001 4, FR-002 3, FR-003 1
(opt-in), FR-004 2, FR-005 4, FR-006 6, FR-007 1, FR-008 5, FR-009 6, FR-010 10, FR-011 7, FR-012 9,
FR-013 4, FR-014 5, FR-015 7, FR-016 4, FR-017 5, FR-018 22, FR-019 5, FR-020 5, FR-021 9, FR-022 10,
FR-023 25, FR-024 13, FR-025 39, FR-026 40, FR-027 7, FR-028 8, FR-029 15, FR-030 7, FR-031 15,
FR-032 1, FR-033 3, FR-034 11, FR-035 9, FR-036 6, FR-037 8, FR-038 3, FR-039 2, SC-001 6, SC-002 37,
SC-003 12, SC-004 1, SC-006 2, SC-007 2.

Support files: `conftest.py` (fixtures `h` / `tree` / `mod`, FR summary, `HELDOUT_REPORT`), `_h7.py`
(harness: temp HOME/HELDOUT_ROOT/TMPDIR per test, canary git repo `toymod`, suite writer with an
observation prelude, fake `uv`, `python3` shim, leak scanner, process reaper), `pytest.ini`.

## How the tests observe the runner

- Canary target repos are tiny git repos (`toymod/__init__.py`) built per test in `/tmp/h7<token>-*`.
- Canary hidden suites are written per test under `<temp HELDOUT_ROOT>/demo/001-demo/` (mode 0700).
  Inner tests record what they see (their `__file__`, cwd, argv, `sys.executable`, PYTHONPATH and
  the sha256 of any `heldout_run.py` on it, `HELDOUT_*` env, a listing of the copy) into
  `$CANARY_OUT`, an env var the runner passes through to pytest.
- Profiles `moeka` / `awork-resume` are exercised with a fake `uv` first on PATH that logs argv,
  obeys `FAKE_UV_SYNC=ok|fail|hang` for `sync`, and execs `python -m pytest` for `run`. Offline.
- Redaction: every canary string is a unique token, and the temp base directory name carries a
  token, so any leaked path is also a leaked token. The feedback channel (stdout, stderr, the
  `--feedback-file` and report `feedback.json`) is scanned for every token, the base path, and
  `(?<![\w-])/[A-Za-z_.~]` (an absolute path, not a count such as `2/6` or `-/4`).
- Malicious maps: an `fr_report.py` whose module-level code writes a sentinel file (must not run,
  MAP still used), and a `MAP` literal containing a call (must give `E_MAP`, nothing evaluated).

## Requirements judged untestable or only partly testable

- **FR-001 "runs on Python 3.11+"**: only the interpreter running the suite is used (3.12 here).
- **FR-003** (awork-resume vendoring, commit on `origin`): needs the awork-resume tree. Only an
  opt-in test (`HELDOUT_007_AWORK=<throwaway copy of awork-resume>`): byte-identical files, SOURCE is
  one 40-hex line, awork `run-heldout.sh` unchanged. "Commit exists on origin" is not checked
  (offline).
- **FR-004**: moeka half checked against the sha256 of `scripts/run-heldout.sh` at
  `consolidate/new-main` `c5340d24`; awork half only in the opt-in test.
- **FR-011 default timeouts** (5400 / 1800 / 600 s) and `--sync-timeout` default 900: not observable
  in reasonable time; only the overrides are tested.
- **FR-013 "SIGKILL after 10 s"**: only the SC-004 bound (ends within timeout + 15 s, plus 8 s slack
  for runner start-up) and that pytest and a child in its process group are dead.
- **FR-016 mechanism** (`flock` on `<report root>/.lock`): only behaviour (second run `E_BUSY`, not
  counted, other feature not blocked).
- **FR-028 meta field names** are not specified: tests check that meta JSON contains the runner
  sha256, the resolved commit (repo mode), `status`, the feature, and that a suite sha256 changes when
  the suite changes; test records must have the seven listed keys.
- **FR-039**: only a keyword check (sandbox / adversarial / hostile) in the docstring and `--help`.
- **SC-005** (parity on moeka `005-multi-instance` and awork `007-data-retention` accepted suites):
  needs the real repos and the real held-out root, which the contract forbids. Covered in spirit by
  the US1-3 test (`fr_report.py`-style canary suite: runner counts equal the canary `fr_report.py`
  run on the report's `junit.xml`). Head task T017.
- **FR-010 on signals**: exit code after SIGINT/SIGTERM is unspecified; only cleanup, dead pytest
  group and no leak are asserted.

## Surface assumptions (interpretations where the spec leaves room)

- `rounds` for a feature without `rounds.json`: exit 0, empty stdout. `rounds --reset` without
  `--reason` is `E_USAGE`. Reset of an empty history is not tested.
- Usage errors print nothing on stdout; no arguments at all is `E_USAGE`; `--help` exits 0 on stdout.
- `main(argv)` returning the int is expected; raising `SystemExit` with the same code is tolerated.
- Held-out root inside the tree: either `E_SUITE_IN_TREE` or `E_REPORT_IN_TREE` accepted.
- `--scratch` inside the tree: either the copy lands outside the tree (exit 0) or the run is refused
  (exit 2 or 3, one stderr line); the tree must be unchanged either way.
- "Inside" is resolved through symlinks (a symlinked suite or runner pointing into the tree is
  refused).
- The plugin directory is the PYTHONPATH entry right after the profile entries, holds a byte copy of
  `heldout_run.py` during the run, is outside copy/suite/tree, and is gone after the run.
  `HELDOUT_ROUND` is the decimal round (`"1"`, `"2"`). The generic cwd is an empty dir under the
  scratch base and is gone afterwards.
- Profile argv: moeka sync is exactly `uv sync --project <copy> --extra dev -q`; launchers start with
  `uv run --project <copy> --extra dev pytest`. awork-resume PYTHONPATH starts `<copy>/src`, `<copy>`.
- `--pytest-arg=VALUE` (equals form) is how dash-leading values are passed; they are the last argv.
- Everything under `<root>/_reports` (dirs incl. `_reports`, `<repo>`, `<feature>`, round dirs; files
  incl. `rounds.json` and any lock file) is 0700 / 0600.
- `report.json` has a top-level `meta` and `tests`; the string `heldout-report.v1` appears in it.
- Triage: default round is the latest run; output starts with the banner; a header per failing test
  `== <one of its ids>: <nodeid>`; `--json` (FR-038 erratum 2026-10-01) is exactly one object
  `{"schema": "heldout-triage.v1", "entries": [{id, nodeid, assertion, spec}]}` with no banner; triage changes nothing under the held-out root
  except possibly `_reports/<repo>/<feature>/.lock`.
- `extract_requirement`: the returned text includes the bullet line; `FR-001` must not match
  `- **FR-001a**`; indented list items are found and end at the next item at the same or lower
  indent; a blank line followed by a line indented deeper than the item continues it.
- Collection error also covers "pytest exited 0 without any result" (`os._exit(0)` in
  `pytest_sessionstart`) and a missing code-under-test package.
- `summarize` sets `cap_reached` when `round == cap` and status is not `passed`, and normalises
  invalid ids to `INVALID_ID` / empty ids to `UNMAPPED`. `ID_PATTERN` may be a string or compiled.
- `load_id_map` accepts a `Path`, returns tuple values; a malformed map raises (any exception).
- Duplicate ids on one test count once (ids are a union).
- Cap refusal applies even when the last round passed; `--no-count` runs are allowed at the cap and
  print `round -/<cap>`; the refused run creates no `round-05` directory.
- The harness sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` for the runner (inherited by the inner
  pytest) so third-party plugins installed on the host do not perturb inner runs, and
  `PYTHONUSERBASE` when pytest itself comes from the user site (temp HOME would hide it).

## Baseline (consolidate/new-main c5340d24, no runner), 2026-10-01

`1 passed, 1 skipped, 251 errors`: every test errors at setup with "scripts/heldout_run.py is
missing" except `test_run_heldout_sh_unchanged` (FR-004, passes: the file is unchanged) and the
opt-in awork test (skipped).

## Validation of the suite itself

The tester checked the suite against a private throwaway implementation written from the spec
(deleted afterwards, never shared): 252 passed + 1 skipped (opt-in FR-003) including slow tests,
about 3.5 minutes; seeded mutants were
caught (stderr leak of pytest output, no process-group kill, executing `fr_report.py`, counting
skipped tests, skipping the `git worktree list` check, no round cap).
