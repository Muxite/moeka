# Feature Specification: Held-out Runner (`heldout-run`)

**Feature Branch**: `007-heldout-runner`

**Created**: 2026-10-01

**Status**: Draft (implementation-ready; held-out pattern)

**Input**: Plan step M10 and `004` FR-016, SC-007, tasks T013-T015: one reusable tool that runs a hidden pytest
suite against a throwaway copy of an implementer's tree under timeouts, maps failures to requirement ids, and emits
only redacted feedback (`FR-xxx: n/m failing` plus totals); full details go only to a private report for the head
agent. Used by moeka and awork-resume. Adds a tester-bug triage view and round tracking (cap 4). The accepted-suite
runners `scripts/run-heldout.sh` in both repos stay as they are.

**Held-out pattern**: the runner is itself built under it. A tester writes hidden tests from this spec only; a
different implementer builds from it. Hidden tests use only the [Test Harness Contract](#test-harness-contract).

## Decision: where it lives

- **Choice**: one standalone, stdlib-only Python file. Canonical copy `scripts/heldout_run.py` in moeka with the
  executable wrapper `scripts/heldout-run`; awork-resume vendors a byte-identical copy of both at the same paths.
  It is not a `moeka` package module.
- **Why not `moeka.heldout`**: awork-resume's constitution (VII) says it imports nothing from moeka, so a package
  module would force a dependency it forbids. The runner also reads `HOME`, spawns processes and walks other
  repositories, which the kernel package may not do (moeka constitution I, zero ambient reads); keeping it in
  `scripts/` keeps the kernel AST guard and import boundary untouched.
- **RSI**: the harness repo (`moeka-rsi/`) vendors the same file or calls the CLI. The candidate kernel helper of
  `004` T015 is answered by this file; no kernel API is added.
- **Trust**: the head agent runs the runner from its own checkout (or the moeka commit it vendored), never from the
  tree under test, because the implementer could edit a copy inside its tree.

## Terms

- **Head**: the agent (or owner) that runs hidden suites and relays feedback. **Implementer**: the agent whose tree
  is tested. **Tester**: the agent that wrote the suite.
- **Held-out root**: `--heldout-root`, else env `HELDOUT_ROOT`, else `$HOME/projects/.heldout`. The default suite of
  repo `R`, feature `F` is `<root>/R/F/`.
- **Tree under test**: a git ref of a repository (`--repo PATH --ref REF`) or a working tree (`--worktree PATH`).
- **Copy**: a throwaway directory holding the tree under test, created for one run and deleted after it.
- **Requirement id**: a string matching `^(FR|NFR|SC)-[0-9]{3}[a-z]?$`. Labels `UNMAPPED` and `INVALID_ID` are also
  reported.
- **Feedback channel**: what the head may forward to the implementer verbatim: the runner's stdout, stderr and exit
  code, and the feedback JSON file.
- **Private report**: the per-run directory with full details, readable by the head only.
- **Round**: one counted run of a feature's suite against an implementer tree.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a hidden suite and get redacted per-requirement feedback (Priority: P1)

The head points the runner at an implementer's worktree and a feature; the runner copies the tree, runs the suite
under a timeout, and prints only failing requirement ids with counts.

**Why this priority**: it replaces the manual RUN.md procedure that every held-out feature repeats by hand.

**Independent Test**: a temp git repo with a tiny module, a temp suite of 6 tests marked with two FR ids where 2
tests of one id fail; run with `--profile generic`; compare stdout with the expected three lines.

**Acceptance Scenarios**:

1. **Given** a suite whose `FR-002` tests fail 2 of 4 and whose `FR-001` tests all pass, **When** the head runs
   `heldout-run run --profile generic --repo-name demo --feature 001-demo --worktree W --suite S`, **Then** stdout is
   exactly `heldout-run: 001-demo round 1/4: failed`, `FR-002: 2/4 failing`, `total: 2/6 tests failing, 0 skipped;
   1/2 ids failing`, and the exit code is 1.
2. **Given** the same suite with every test passing, **When** run, **Then** the exit code is 0 and no id line is
   printed.
3. **Given** an awork-resume style suite with no markers and a `fr_report.py` holding `MAP`, **When** run, **Then**
   ids come from `MAP` and the counts equal what `fr_report.py` prints for the same junit file.

---

### User Story 2 - Nothing about the tests leaks (Priority: P1)

Test names, assertion text, inputs, fixture values and paths never appear in the feedback channel, whatever the
outcome, and the suite never sits inside a worktree.

**Why this priority**: it is the whole point of the held-out pattern (`004` SC-007).

**Independent Test**: a canary suite whose every name, message, literal, fixture value, marker argument and printed
line carries a unique token; run it in each outcome (pass, fail, timeout, collection error, sync failure, refusal)
and grep the feedback channel for tokens and paths.

**Acceptance Scenarios**:

1. **Given** the canary suite, **When** it fails, **Then** stdout, stderr and the feedback JSON contain zero canary
   tokens and zero absolute paths.
2. **Given** a suite directory inside the implementer's worktree, **When** the head runs it, **Then** the runner
   refuses with `heldout-run: error E_SUITE_IN_TREE` and exit 2 before copying anything.

---

### User Story 3 - Triage a failure against the spec (Priority: P2)

The head sees a failing test's assertion next to the spec text of the requirement it claims to prove, to decide
"implementer bug" or "tester bug".

**Why this priority**: tester bugs are common and must go back to the tester, not the implementer.

**Independent Test**: after a failed run, `heldout-run triage --id FR-002 --spec spec.md`; the output shows the
assertion message and the `FR-002` bullet text.

**Acceptance Scenarios**:

1. **Given** round 1 failed on `FR-002`, **When** triage runs with `--id FR-002`, **Then** each failing test of
   `FR-002` is shown with its node id, its failure message and the full `FR-002` requirement text from the spec.

---

### User Story 4 - Rounds stop at the cap (Priority: P2)

Each counted run is a round; at the cap the loop stops and escalates to the owner.

**Why this priority**: the standing rule is round cap 4.

**Independent Test**: run a failing suite four times, then a fifth; the fourth prints the escalation line, the fifth
is refused with exit 4.

**Acceptance Scenarios**:

1. **Given** 4 counted rounds, **When** a fifth counted run starts, **Then** it is refused with `E_ROUND_CAP`, exit 4,
   and no test runs.
2. **Given** a head re-run with `--no-count`, **When** it finishes, **Then** the round count is unchanged.

### Edge Cases

- The implementer tree already contains `tests/heldout/<feature>/` (FR-009).
- A marker argument that is not a requirement id, for example free text (FR-019).
- Pytest cannot import the code under test (collection error) (FR-015).
- The suite hangs or ignores SIGTERM (FR-013).
- Dependency sync fails (FR-014).
- Two runs of the same feature at once (FR-016).
- The runner is interrupted with SIGINT (FR-010).
- The held-out root is group- or world-readable (FR-031).

## Requirements *(mandatory)*

### Functional Requirements

#### Packaging and trust

- **FR-001**: The runner MUST be one Python file, `scripts/heldout_run.py`, that runs on Python 3.11+ and imports at
  module level only names in `sys.stdlib_module_names`; it MUST NOT import `moeka`, `nanobot`, `awr` or any
  third-party package (it may import `pytest` only inside the pytest-plugin hooks it defines). It MUST be importable
  as module `heldout_run` from its directory.
- **FR-002**: `scripts/heldout-run` MUST be an executable wrapper that runs `heldout_run.py` with `python3` and passes
  all arguments. `heldout-run --version` MUST print exactly `heldout-run <version> sha256:<hex>` where `<hex>` is
  the sha256 of the bytes of `heldout_run.py`, and exit 0.
- **FR-003**: awork-resume MUST carry byte-identical copies of both files at the same paths, vendored from a moeka
  commit that exists on `origin` (constitution, pins resolve from origin); the commit id is recorded in
  `scripts/heldout_run.SOURCE` (one line: the 40-hex moeka commit).
- **FR-004**: `scripts/run-heldout.sh` in both repos MUST stay byte-identical to the versions before this feature.
- **FR-005**: In `--worktree` mode the runner MUST refuse (`E_RUNNER_IN_TREE`, exit 2) when its own file resolves
  inside the tree under test.

#### Tree copy and suite placement

- **FR-006**: `--repo PATH --ref REF` MUST create the copy from `git -C PATH archive REF` extracted into a new empty
  directory; the resolved commit id is recorded in the private report. A failing `git` gives `E_GIT`, exit 2.
- **FR-007**: `--worktree PATH` MUST copy the working tree as it is (committed, modified and untracked files),
  preserving symlinks as symlinks, and excluding every path with a component named `.git`, `.venv`, `__pycache__`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache` or `node_modules`, and files ending in `.pyc`.
- **FR-008**: The copy MUST NOT contain `.git` in either mode, and MUST be created under `--scratch` (default: the
  system temp directory), never inside the tree under test, the suite or the held-out root.
- **FR-009**: The suite MUST be copied to `<copy>/tests/heldout/<feature>/` (any existing directory there is removed
  first), unless `--suite-dest REL` names another path relative to the copy. The suite source MUST NOT be modified:
  no file in it is created, changed or removed by a run.
- **FR-010**: Every scratch directory of a run (copy, plugin directory, records) MUST be removed when the run ends,
  whether it passed, failed, timed out, hit an error, or the runner received SIGINT or SIGTERM. `--keep` keeps the
  copy; its path is written only to the private report.

#### Execution

- **FR-011**: `--profile` MUST select defaults; explicit flags override them:

  | Profile | Sync step | Pytest launcher | `PYTHONPATH` entries | Working dir | Timeout |
  |---|---|---|---|---|---|
  | `moeka` | `uv sync --project {copy} --extra dev -q` | `uv run --project {copy} --extra dev pytest` | `{copy}` | new empty scratch dir | 5400 s |
  | `awork-resume` | none (`uv run` syncs) | `uv run --project {copy} --extra dev pytest` | `{copy}/src`, `{copy}` | `{copy}` | 1800 s |
  | `generic` | none | `<runner's sys.executable> -m pytest` | `{copy}` | new empty scratch dir | 600 s |

  Overrides: `--timeout S`, `--sync-timeout S` (default 900), `--no-sync`, `--pytest-arg ARG` (repeatable, appended),
  `--unset VAR` (repeatable, removed from the child environment).
- **FR-012**: Pytest MUST be invoked with `<suite copy> -q -p no:cacheprovider -p heldout_run
  --junitxml=<report dir>/junit.xml` plus `--pytest-arg` values. The child environment is the runner's environment
  minus `--unset` names, plus `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH` (profile entries, then the plugin directory,
  then any inherited value), `HELDOUT_RUN_RECORDS=<scratch file>` and `HELDOUT_ROUND=<n>`. The plugin directory holds
  a copy of `heldout_run.py`, so `-p heldout_run` loads the runner's own plugin hooks.
- **FR-013**: The timeout MUST be wall-clock from pytest start. On expiry the runner sends SIGTERM to pytest's process
  group, then SIGKILL after 10 s. Status is `timeout`; every collected test without a final result counts as failing.
- **FR-014**: A sync step that fails or exceeds `--sync-timeout` MUST end the run with status `infra_error`, exit 3,
  and no round counted. Busy (FR-016) and any other runner-side failure after argument checks are also
  `infra_error`.
- **FR-015**: When pytest reports collection errors, or exits with code 2, 3, 4 or 5, or no test result is recorded,
  the status MUST be `collection_error`, exit 3, and the round counted.
- **FR-016**: At most one run per (repo name, feature) at a time: a second concurrent run MUST end at once with
  `E_BUSY` on stderr, status `infra_error`, exit 3, and no round counted (an exclusive lock on the feature's report
  root).

#### Mapping tests to requirement ids

- **FR-017**: A test's ids MUST be the union of (a) the arguments of every `fr` marker on the test item and (b) the
  id-map entries for the test's key. The key is the test function name without parametrisation (`item.originalname`),
  which equals a junit `testcase` `name` with any `[...]` suffix removed.
- **FR-018**: The id map MUST come from `--map FILE` when given, else `<suite>/SPEC-MAP.json` when present, else
  `<suite>/fr_report.py` when present, else no map. A `.json` map is an object `{test key: [id, ...]}` (optionally
  wrapped as `{"map": {...}}`). A `.py` map is the top-level assignment `MAP = {...}` (annotated or not), read with
  `ast.literal_eval` of that node only; the file is never imported or executed. An unreadable or malformed map gives
  `E_MAP`, exit 2.
- **FR-019**: An id that does not match the requirement-id pattern MUST be counted under `INVALID_ID` and never
  echoed. A test with no id MUST be counted under `UNMAPPED`. A test with several ids counts under each.
- **FR-020**: Outcome per test: `failed` when its call failed or its setup or teardown errored (including a timeout
  or a missing result, FR-013); `skipped` when skipped; xfail counts as `passed`, a strict xpass as `failed`;
  otherwise `passed`. For an id, `m` is the number of its tests that are not skipped and `n` the number of those
  that failed.

#### Feedback (the only output the implementer may see)

- **FR-021**: The text feedback on stdout MUST be exactly:
  - line 1: `heldout-run: <feature> round <r>/<cap>: <status>` (`<r>` is `-` for a `--no-count` run);
  - one line `<id>: <n>/<m> failing` for each id with `n > 0`, ordered by prefix (`FR`, `NFR`, `SC`, then
    `INVALID_ID`, `UNMAPPED`), then number, then suffix;
  - `total: <f>/<t> tests failing, <k> skipped; <i>/<j> ids failing` (`t` excludes skipped tests, `j` counts ids
    and labels with `m > 0`);
  - when the run reaches the cap without passing, a last line `round cap reached: escalate to owner`.
  `<status>` is one of `passed`, `failed`, `timeout`, `collection_error`, `infra_error`.
- **FR-022**: `--json` MUST print instead one JSON object with exactly the keys `schema` (`"heldout-feedback.v1"`),
  `feature`, `round` (int or `null`), `round_cap`, `status`, `cap_reached` (bool), `failing` (object: id ->
  `{"failing": n, "total": m}`, only ids with `n > 0`), `totals` (`{"tests", "failing", "skipped", "ids",
  "ids_failing"}`). `--feedback-file PATH` MUST also write that object to `PATH`.
- **FR-023**: The feedback MUST be built only from the feature name, round numbers, the status word, ids or labels
  and integers. The feature name MUST match `^[0-9]{3}-[a-z0-9][a-z0-9-]*$` and the repo name
  `^[a-z0-9][a-z0-9_-]{0,63}$`, else `E_FEATURE`, exit 2.
- **FR-024**: Pytest's stdout and stderr, the junit file, tracebacks, the sync log and the runner's own diagnostics
  MUST NOT reach the feedback channel; they go only to the private report.
- **FR-025**: stderr MUST be empty except for exactly one line `heldout-run: error <CODE>` when the run is refused or
  hits an infra error, with `<CODE>` one of `E_USAGE`, `E_FEATURE`, `E_SUITE_MISSING`, `E_SUITE_IN_TREE`,
  `E_SUITE_PERMS`, `E_REPORT_IN_TREE`, `E_RUNNER_IN_TREE`, `E_MAP`, `E_GIT`, `E_SYNC`, `E_BUSY`, `E_ROUND_CAP`.
  Argument-parsing errors print `E_USAGE` only (no usage text echoing argument values).
- **FR-026**: Exit codes: 0 passed; 1 failed; 2 refused (usage, feature or repo name, unsafe placement, map, git); 3
  timeout, collection error or infra error; 4 round cap reached (refused before running).

#### Private report

- **FR-027**: The report directory MUST be `--report-dir` when given, else `<heldout root>/_reports/<repo>/<feature>/
  round-<NN>/` for counted runs (`NN` two-digit, zero-padded) and `.../run-<UTC yyyymmddThhmmssZ>/` for `--no-count`
  runs. Directories are created mode 0700 and files 0600.
- **FR-028**: The report directory MUST contain `report.json` (schema `heldout-report.v1`: `meta` with runner
  version and sha256, repo, feature, round, tree source (mode, ref, commit or worktree path), suite path and sha256
  of its file tree, the pytest command, start time, duration, status, pytest exit code; and `tests`, a list of
  `{nodeid, key, ids, outcome, when, duration_s, longrepr}`), `junit.xml`, `pytest.log` (pytest stdout and stderr),
  `sync.log` (when a sync step ran) and `feedback.json` (the FR-022 object).
- **FR-029**: The suite directory, the report directory and the held-out root MUST NOT be inside (a) the tree under
  test, (b) any worktree of the repository (`git -C <repo> worktree list --porcelain`, where `<repo>` is `--repo` or
  the worktree's repository), or (c) any `--forbid-under PATH`. A violation is refused before anything is copied
  (`E_SUITE_IN_TREE` or `E_REPORT_IN_TREE`, exit 2).
- **FR-030**: `heldout-run check-isolation --suite S [--repo R] [--forbid-under P]...` MUST apply the FR-029 and
  FR-031 checks without running anything: exit 0 when isolated, else exit 2 with the error code line. The RSI
  harness passes its container mount sources as `--forbid-under`.
- **FR-031**: The suite directory and the held-out root MUST have no group or other permission bits (`mode & 0o077 ==
  0`), else `E_SUITE_PERMS`, exit 2. A missing suite gives `E_SUITE_MISSING`, exit 2.

#### Rounds

- **FR-032**: Round state MUST live in `<heldout root>/_reports/<repo>/<feature>/rounds.json` (schema
  `heldout-rounds.v1`: `cap`, `rounds` list of `{n, started_at, status, failing_ids, tree}`, `resets` list of `{at,
  reason, rounds}`). A counted run appends round `n = len(rounds) + 1`.
- **FR-033**: A run MUST be counted when its status is `passed`, `failed`, `timeout` or `collection_error`, and not
  counted for `infra_error`, refusals, `--no-count` runs and `triage`.
- **FR-034**: The cap is `--cap N` (default 4). A counted run when `len(rounds) >= cap` MUST be refused with
  `E_ROUND_CAP`, exit 4, before anything is copied. The counted run that makes `len(rounds) == cap` without
  passing MUST print the escalation line and set `cap_reached: true`.
- **FR-035**: `heldout-run rounds --repo-name R --feature F` MUST print one line per round, `round <n>: <status>
  <ids...>` (ids only). `--reset --reason TEXT` MUST move the current rounds into `resets` with the reason (required)
  and start again at 0. [NEEDS CLARIFICATION: when the head finds that a failing round was caused by a tester bug,
  does that round still count toward the implementer's cap? Recommended default: it counts unless the head resets
  with `--reason tester-bug`; the history is kept in `resets`, so the owner sees every reset.]

#### Tester-bug triage (head only)

- **FR-036**: `heldout-run triage --repo-name R --feature F [--round N] [--id ID]... --spec PATH` MUST read the
  private report of round `N` (default: the latest run) and print, for each failing test (filtered to the given ids
  when `--id` is used): a header `== <id>: <nodeid>`, a block `assertion:` with the failure message (the lines of
  `longrepr` starting with `E `, else its last 20 lines; at most 4000 characters), and a block `spec:` with the
  requirement text of every id the test carries.
- **FR-037**: The requirement text of an id is the markdown list item in the spec file that starts with `- **<id>**`
  (allowing a parenthesised suffix inside the bold, such as `- **FR-016 (held-out ...)**`), up to but not including
  the next line that starts a list item at the same or lower indent, a heading, or a blank line followed by a
  non-indented line. When absent, the block reads `(not found in spec)`.
- **FR-038**: Triage output MUST start with the line `PRIVATE TRIAGE: not for the implementer`; `--json` gives
  schema `heldout-triage.v1` (`[{id, nodeid, assertion, spec}]`). Triage never runs tests, never counts a round and
  never writes a feedback file.

#### Stated limits

- **FR-039**: The runner MUST NOT claim adversarial isolation. The suite runs in the same process as the
  implementer's code, which can read the suite copy, write outside the copy, or tamper with results; the feedback
  counts are a low-bandwidth channel. The runner prevents accidental exposure (placement, redaction, cleanup), not a
  hostile implementer. This limit MUST be stated in the module docstring and `--help`. [NEEDS CLARIFICATION: require
  an OS sandbox (for example bubblewrap with the copy as the only writable path and no network) for every run?
  Recommended default: no for agent implementers (cooperative threat model); the RSI harness already isolates the
  mutator in containers, and the head can wrap the runner when needed.]

### Key Entities

- **Run**: one invocation of `heldout-run run`; has a tree source, a suite, a status and a report directory.
- **Test record**: node id, key, ids, outcome (private).
- **Feedback**: ids with counts and totals (public).
- **Round state**: counted runs per (repo, feature) with resets.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On fixture repos and suites with known failures, the feedback lists exactly the failing ids with exact
  `n/m`, in marker mode, `SPEC-MAP.json` mode and `fr_report.py` mode (100% of cases).
- **SC-002**: Redaction (`004` SC-007): for a canary suite run in every status (passed, failed, timeout,
  collection_error, infra_error) and every refusal, the feedback channel contains 0 canary tokens (test names,
  parametrise ids, assertion messages, literals, fixture values, marker free text, printed output, file names) and
  0 matches of an absolute path (`/` followed by a path segment, for example the suite, copy, report or held-out root
  paths).
- **SC-003**: After any run, the source repository or worktree and the suite directory are unchanged (same file set
  and bytes), no scratch directory of the run remains, and the suite is not inside any worktree or forbidden path.
- **SC-004**: A suite that sleeps forever and ignores SIGTERM ends within `timeout + 15 s` with status `timeout`.
- **SC-005**: moeka's accepted suite `005-multi-instance` (markers) and awork-resume's accepted `007-data-retention`
  (`fr_report.py`) run through `heldout-run` on their accepted refs with 0 failing ids, and their per-id totals
  equal those of their own conftest summary and `fr_report.py`.
- **SC-006**: The fifth counted run of a feature with cap 4 is refused with exit 4; `--no-count` runs never change
  the count.
- **SC-007**: For a failing test, triage output contains its failure message and the full spec text of each of its
  ids.

## Assumptions

- `git`, `python3` (3.11+) and, for the `moeka` and `awork-resume` profiles, `uv` are on `PATH`.
- Suites are pytest suites; other runners are out of scope.
- The head forwards only the feedback channel; anything else it reads (report, triage) stays with the head.
- Held-out tests are not committed during development; accepted suites move to `tests/heldout/<feature>/` and run
  with `scripts/run-heldout.sh` as today.

## Test Harness Contract

Hidden tests MUST use only these surfaces, MUST run offline, and MUST NOT touch the real `$HOME`, the real held-out
root or the live checkout. Tests set `HOME`, `HELDOUT_ROOT` and `TMPDIR` to temp directories and use
`--profile generic`, so the suite under test runs with the test process's own interpreter (which has pytest).

### CLI

- `scripts/heldout-run` (or `python3 scripts/heldout_run.py`) with subcommands:
  - `run --repo-name R --feature F (--worktree P | --repo P --ref REF) [--profile moeka|awork-resume|generic]
    [--suite S] [--suite-dest REL] [--heldout-root D] [--report-dir D] [--scratch D] [--map FILE] [--timeout S]
    [--sync-timeout S] [--no-sync] [--pytest-arg A]... [--unset V]... [--forbid-under P]... [--cap N] [--no-count]
    [--keep] [--json] [--feedback-file F]`;
  - `triage --repo-name R --feature F [--round N] [--id ID]... --spec PATH [--json]`;
  - `rounds --repo-name R --feature F [--reset --reason TEXT]`;
  - `check-isolation --suite S [--repo R] [--forbid-under P]...`;
  - `--version`.
- Exit codes and stderr codes as in FR-025 and FR-026.

### Python API (module `heldout_run`, loaded from the file path)

- `main(argv: list[str]) -> int` (the CLI).
- Helpers: `load_id_map(path) -> dict[str, tuple[str, ...]]`; `summarize(records, *, feature,
  round, cap, status) -> dict` (the FR-022 object); `format_feedback(feedback: dict) -> str` (the FR-021 text);
  `extract_requirement(spec_text: str, req_id: str) -> str | None` (FR-037); constants `ID_PATTERN`,
  `FEEDBACK_SCHEMA = "heldout-feedback.v1"`, `ERROR_CODES` (the FR-025 set).
- A test record passed to `summarize` is a mapping `{"nodeid", "key", "ids", "outcome"}` with `outcome` in `passed`,
  `failed`, `skipped`.

### Environment variables

- Read by the runner: `HELDOUT_ROOT`, `HOME` (default root only), `TMPDIR` (default scratch).
- Set for pytest: `HELDOUT_RUN_RECORDS`, `HELDOUT_ROUND`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPATH`.

### File locations

- Suite: `<root>/<repo>/<feature>/` (default), with optional `SPEC-MAP.json` or `fr_report.py` (`MAP`).
- Private report: `<root>/_reports/<repo>/<feature>/round-NN/` or `run-<ts>/` with `report.json`, `junit.xml`,
  `pytest.log`, `sync.log`, `feedback.json`.
- Round state: `<root>/_reports/<repo>/<feature>/rounds.json`.
- Repo files: `scripts/heldout_run.py`, `scripts/heldout-run`, `scripts/heldout_run.SOURCE` (awork-resume),
  `scripts/run-heldout.sh` (unchanged).
