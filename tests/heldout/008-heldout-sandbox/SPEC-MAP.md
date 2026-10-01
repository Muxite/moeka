# SPEC-MAP: held-out tests for spec 008-heldout-sandbox

Source: `specs/008-heldout-sandbox/spec.md` plus the still-binding 007 Test Harness Contract
(`specs/007-heldout-runner/spec.md`). Only contract surfaces are used: the `run` CLI with the
008 flags, the module constants, env vars, file locations and the private `report.json`, plus
Docker itself (image tag scheme). 146 collected tests across 7 files; every test carries
`@pytest.mark.fr(...)`, and the conftest summary groups results by id.

Most behaviour tests need Docker. They are marked `docker` and run **unsandboxed by the head**
(the head has Docker). They build toy trees and toy suites in temp dirs and call
`heldout_run.py run --sandbox docker ...`. A toy suite runs INSIDE the sandbox, so it cannot
write host paths; the outer test observes it through the private `report.json` test outcomes
(helpers `real_tests`, `outcomes`, `assert_toys_passed` in `_h8.py`). A handful of tests assert
image contents directly with `docker run --entrypoint /bin/sh <image>` (`_h8.in_image`).

| Group | File | FRs / SCs | Collected |
|-------|------|-----------|-----------|
| A mode, image, availability, limits, unchanged files, API | `test_a_mode_image.py` | FR-001,002,003,018,019,020 + API | 51 |
| B build phase (sync container as impl, net, limits) | `test_b_build_phase.py` | FR-004,005,006,009 | 18 |
| C test phase (net none, ro root, /work /suite /out, judge, timeout, cleanup, docker marker) | `test_c_test_phase.py` | FR-006..010,012,019 | 31 |
| D impl vs judge (heldout-impl, unreadable judge files, isolation flag) | `test_d_separation.py` | FR-011,012,013, SC-005 | 19 |
| E canaries, count check, tamper detection, tampered feedback | `test_e_tamper.py` | FR-014..017, SC-004 | 13 |
| F exfiltration / network (Story 2) | `test_f_exfiltration.py` | FR-004,006,009,010, SC-003 | 7 |
| G feedback parity, perf, accepted-suite parity | `test_g_parity.py` | SC-001,002,006, FR-019 | 7 |

Collected per id (a test counts under every id it carries): FR-001 7, FR-002 13, FR-003 3,
FR-004 15, FR-005 15, FR-006 12, FR-007 7, FR-008 1, FR-009 10, FR-010 10, FR-011 5, FR-012 12,
FR-013 3, FR-014 5, FR-015 1, FR-016 7, FR-017 6, FR-018 24, FR-019 7, FR-020 3, SC-001 4,
SC-002 2, SC-003 5, SC-004 5, SC-005 2, SC-006 1.

Support files: `conftest.py` (fixtures `h`/`tree`/`mod`/`image`/`no_new_containers`, FR summary,
`HELDOUT_REPORT`), `_h8.py` (harness: temp HOME/HELDOUT_ROOT/TMPDIR per test, toy git repo
`toymod`, suite writer, docker helpers, report parsers, token search, process+container reaper),
`pytest.ini`.

## How the tests observe the sandbox

- **In-sandbox self-checks (Story 4, FR-004..007, FR-011/012).** The toy suite's own test bodies
  call `os.getuid()`, read `/proc/self/mountinfo`, `/proc/self/status` (capabilities, NoNewPrivs),
  `/sys/fs/cgroup/{memory,cpu,pids}.max`, `/sys/class/net`, open sockets, and run
  `os.environ["HELDOUT_IMPL"] id -u` / `cat /suite/...`. They assert inside the container and
  report via pass/fail; the outer test reads those outcomes from `report.json` (canary tests
  excluded). Every in-container cgroup/mountinfo/caps parsing snippet was validated against a real
  `--read-only --tmpfs /tmp --network none --memory --cpus --pids-limit --cap-drop ALL
  --security-opt no-new-privileges` container (net `['lo']`, root `ro`, `/tmp` tmpfs,
  `memory.max=536870912` for `512m`, `cpu.max=['100000','100000']` for `--cpus 1`,
  `CapEff=0`, `NoNewPrivs=1`, connect blocked).
- **Build-phase facts.** A toy `--sync-cmd` script writes what it sees (uid, mounts, caps, cgroup
  limits, network ifaces, `/work` writability) into `/work/_facts`; the test phase then asserts on
  those facts and reports pass/fail.
- **Exfiltration (SC-003).** Each toy suite file carries a unique `SUITE_CANARY <token>`. After the
  run, `_h8.search_tokens` scans the worktree, scratch TMPDIR, HOME and held-out root (excluding the
  private report dir) for the token and for files the tree tried to write; the test asserts the
  token survives ONLY under the private report dir (a positive control checks it IS found there).
- **Tamper (SC-004, FR-016).** Five hostile toy suites forge results the five ways SC-004 lists
  (rewrite the records file, monkeypatch the plugin emitter, force `passed` via
  `pytest_runtest_makereport`, `os._exit(0)` early, delete the junit). Each must end `tampered` or
  `collection_error`, never `passed`; the explicit-hook and honest cases pin exactly `tampered`.

## Interpretations (spec left room; a test asserting these must be read as the tester's reading)

- **`--sandbox-cpus N` -> cgroup `cpu.max`** is `N*100000 100000` (Docker's default 100 ms CPU
  period). Tested for `N=2` (default) and `N=1`.
- **`--sandbox-memory M` -> `memory.max`** equals the byte value (`512m` -> `536870912`).
- **`--sync-cmd` `{copy}`** expands to the in-container copy path, which is `/work` (FR-004).
  `generic` gains a sync step only when `--sync-cmd` is given (`sync.log` then exists).
- **`meta.image`** names the default or overridden image: the test accepts the tag, the full
  `repo:tag`, or the image id. **`meta.sandbox`** is exactly `"none"` or `"docker"`.
- **`canaries`** in `report.json`: the parser accepts a list of `{name/nodeid, outcome}` objects,
  an object of such objects, or a plain `{name: outcome}` map; it needs two entries with outcomes
  `passed` and `failed`, random-looking names (>=16 hex/length), absent from the feedback, and not
  added to the feedback totals (US1 stays `2/6`). Canary tests carry id `CANARY` or match a canary
  name, and are excluded from the per-id/outcome helpers.
- **`tampered` feedback**: first stdout line ends `: tampered`, no per-id `n/m` lines, and
  `feedback.json`/`--json` has `status:"tampered"` with empty/absent `failing`; exit 1, round
  counted. "Only feature, round, cap and status" (FR-017) is read as: those keys present, no id or
  count content, no token leak.
- **Docker-marker refusal (FR-010)**: a `-m` expression (any `--pytest-arg` form) that contains the
  word `docker` not immediately preceded by `not` is refused `E_SANDBOX_DOCKER` exit 2; `not docker`
  is allowed. Unsandboxed, `-m docker` is never refused.
- **`heldout-impl` child**: `id -u` = 10102, single group `impl`, empty Cap{Inh,Prm,Eff,Bnd,Amb};
  stdin/stdout/stderr/cwd/env pass through except `HELDOUT_RUN_RECORDS` is removed; exit status is
  the child's. `HELDOUT_IMPL=/usr/local/bin/heldout-impl` in sandboxed runs, unset otherwise.
- **`isolation`**: `"partial"` when any `/work` module was imported in the judge process (import at
  collection or inside a test body), else `"full"` (impl reached only as a subprocess via
  `heldout-impl`). Computed inside that process (FR-013), so tested only via `report.json`.
- **Report dir ownership/perms** (FR-009): the copied `/out` tree is owned by the head's uid and is
  0700/0600 (not left owned by `judge`/`impl`).
- **`infra_error` surface** reuses the 007 stderr form `heldout-run: error <CODE>` with the new
  codes `E_SANDBOX_UNAVAILABLE` (exit 3) and `E_SANDBOX_DOCKER` (exit 2). The first feedback line's
  status word is `infra_error`.
- **Daemon unreachable** is simulated with `DOCKER_HOST=unix://<nonexistent>.sock`; **docker
  missing** with a `PATH` that has no `docker`.
- **`--sandbox <other>`** (including empty, `Docker`, `docker,none`) is `E_USAGE` exit 2 with empty
  stdout.

## Untestable or only partly testable from the contract

- **FR-002 image internals beyond the contract** (exact base image, how uv is installed): only the
  observable facts are checked (Python 3.12, uv, pytest, git, users `judge`/`impl` with the stated
  uids, an executable `/usr/local/bin/heldout-impl`) and the tag = `sha256(SANDBOX_DOCKERFILE)[:12]`.
- **FR-012 mechanism** (setuid helper vs root broker vs setpriv): only behaviour — impl uid differs,
  single group, no capabilities on the child, and none held by pytest's own process tree (checked
  for the pytest process and, when it is the parent, the parent) — never the specific mechanism.
- **FR-008/FR-005 timeout "stop -t 10 then kill"**: only the SC-type bound (ends within
  timeout + ~15 s plus runner/stop slack) and the resulting status; the exact `docker stop -t 10`
  call is not observed.
- **FR-006 "read-only root filesystem"** is checked by the `/` mount being `ro`; a kernel-level
  escape is out of scope (the spec states Docker is the boundary, FR-018).
- **SC-006 60 s bound** is measured on this host with a warm image; it is wall-clock and marked
  `slow`.
- **SC-002 parity** (accepted suites `005-multi-instance`, `006-rsi-kernel-prerequisites`) needs the
  real tree and is `slow`+`network` (the moeka profile syncs from PyPI). It copies the tree with
  `git archive HEAD`, copies the accepted suite into a 0700 held-out dir, runs it `--profile moeka`
  both ways with `-m "not docker and not slow"`, and compares per-id counts. It `skip`s when the
  suite or `git archive` is unavailable. The owner may need to vendor/sync deps first.
- **FR-013 flag computed in-process**: asserted only through `report.json`.

## Baseline (spec-008 base commit a79edd11, unsandboxed runner present, no sandbox support)

Run against a `git archive HEAD` copy of the tree under test, 2026-10-01: **16 passed, 29 failed,
101 errors** (146 tests). Docker-marked tests error because the `image` fixture fails fast
(`heldout_run.SANDBOX_DOCKERFILE` missing, no `--sandbox` support). Not-passing per id (a test
counts under each id it carries): FR-001 3/7, FR-002 13/13, FR-003 3/3, FR-004 15/15, FR-005 15/15,
FR-006 12/12, FR-007 7/7, FR-008 1/1, FR-009 10/10, FR-010 9/10, FR-011 5/5, FR-012 11/12,
FR-013 3/3, FR-014 5/5, FR-015 1/1, FR-016 7/7, FR-017 6/6, FR-018 17/24, FR-019 6/7, FR-020 0/3,
SC-001 4/4, SC-002 2/2, SC-003 5/5, SC-004 5/5, SC-005 2/2, SC-006 1/1.

The 16 baseline passers are all behaviour the sandbox leaves unchanged, so they are expected to
stay green: FR-020 (the wrapper and `run-heldout.sh` are byte-identical; the wrapper still runs the
runner), the four `--sandbox <unknown>` usage-error cases (FR-001), the seven FR-018 keyword checks
that overlap the existing 007 docstring/`--help` text, `test_docker_marker_allowed_unsandboxed`
(FR-010/019) and `test_heldout_impl_unset_without_sandbox` (FR-012). FR-020 shows 0 not-passing by
design.

## Validation of the suite itself

No full private implementation was built (a real Docker sandbox is expensive). Cheap self-checks
performed: the suite collects (146 tests), every test carries a valid `fr` marker, all FR-001..020
and SC-001..006 are covered, the `_h8` report parsers (`canary_entries`, `real_tests`, `outcomes`,
`per_id_counts`, `status_of`, `image_tag_for`) pass unit checks on synthetic `report.json` shapes,
and every in-container probe (mountinfo/caps/cgroup/network parsing) was validated against a real
constrained container so the toy-suite check code is correct Python with correct expectations.


## Head amendments

- 2026-10-01 (head, round 1 triage), tester bugs fixed:
  - FR-006 `test_persist_attempts_do_not_escape`: wrote as `judge`, which owns `/suite` and `/out` by spec; now the
    attempts go through `HELDOUT_IMPL`, and the judge writes one note to `/out` (expected in the private report).
  - FR-006 `test_no_docker_socket`: a recursive glob over `/` (incl. /proc) timed out; now bounded dirs plus
    `/proc/self/mountinfo`.
  - FR-004 `test_sync_cannot_see_suite`: `grep -r /` could block on /proc or /sys and outlast the sync timeout;
    now excludes them and is wrapped in `timeout 120`.
  - SC-003 survivor search: excluded the suite's own source dir and the default `_reports` root.
  - FR-015 `test_report_records_collected_count`: required canaries in `tests`; the spec keeps them in `canaries`.
  - FR-004 `test_sync_cannot_see_suite`: flagged empty mount points and the image's `/root`; now flags only
    non-empty `/suite`, `/out`, `/heldout`.
