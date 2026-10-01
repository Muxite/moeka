# Feature Specification: Held-out Runner Sandbox (`heldout-run --sandbox docker`)

**Feature Branch**: `008-heldout-sandbox`
**Created**: 2026-10-01
**Status**: Draft (implementation-ready; held-out pattern)
**Input**: Owner, 2026-10-01: "build it into a sandbox". `007-heldout-runner` FR-039 states the runner is not a
sandbox (the suite runs in the same process as the implementer's code). moeka-rsi decision D1
(`~/projects/moeka-rsi/docs/decisions.md`): moeka agents build tools unattended and are judged by hidden suites,
so the judge must hold against a hostile implementation, not only a cooperative one.

This spec extends `007`. Everything in 007 stays true unless a requirement here says otherwise. Without
`--sandbox` the runner behaves exactly as in 007 (byte-identical feedback, same reports).

## Threat model

The implementation under test (the "tree") is hostile. It may run any code at sync time and at test time. Its
goals: (1) learn the hidden tests (names, assertions, inputs) through anything that outlives the run; (2) make
failing tests count as passing; (3) damage or read anything on the host.

The judge (the runner, the suite, the report) is trusted. The per-id counts in the feedback are an accepted,
low-bandwidth channel (as in 007).

## User Scenarios & Testing

### Story 1 - a sandboxed run gives the same feedback as an unsandboxed run (P1)
**Independent test**: a toy suite with one failing id; `run --sandbox docker` prints the same three feedback lines
as `run` without it.

### Story 2 - the tree cannot see the suite outside the run, or reach the network (P1)
**Independent test**: a hostile toy tree whose sync step and whose test-time code try to read the suite, write
outside the copy, open sockets, and persist files; afterwards nothing of the suite exists anywhere the tree's next
run or the implementer could read, and no connection succeeded.

### Story 3 - a tree that forges results is caught (P1)
**Independent test**: a hostile toy tree that rewrites pytest outcomes in-process (for example a `conftest`-style
import hook or monkeypatching the reporting) gets status `tampered`, not `passed`.

### Story 4 - implementation processes run as a different user (P2)
**Independent test**: inside the test phase, `heldout-impl id -u` differs from the judge's uid, and
`heldout-impl cat <suite file>` fails.

## Requirements

### Mode and image
- **FR-001**: `run --sandbox docker` MUST run the sync step and pytest in Docker containers as below.
  `--sandbox none` (the default) is 007 behaviour. Any other value is a usage error (exit 2).
- **FR-002**: The sandbox image MUST be built from a Dockerfile that the runner writes from a constant embedded in
  `heldout_run.py` (the runner stays one file, 007 FR-001), tagged `heldout-sandbox:<sha256 of the Dockerfile text,
  first 12 hex>`, and built only when that tag is missing. It contains Python 3.12, `uv`, `pytest`, `git`, the
  users `judge` (uid 10101) and `impl` (uid 10102), and the `heldout-impl` launcher (FR-012).
  `--sandbox-image IMAGE` overrides it (the image must provide the same users and launcher).
- **FR-003**: `docker` missing or the daemon unreachable MUST end the run with status `infra_error`, exit 3, stderr
  `heldout-run: error E_SANDBOX_UNAVAILABLE`, no round counted.

### Build phase (untrusted code, network, no suite)
- **FR-004**: The sync step (the profile's, or `--sync-cmd CMD`, a new flag; `{copy}` is replaced by the copy's
  in-container path) MUST run in a container from the sandbox image with the copy mounted read-write at `/work`,
  as uid `impl`, with the default bridge network (registries reachable). Neither the suite, the report directory,
  the held-out root nor any other host path is mounted. `generic` gains no sync step unless `--sync-cmd` is given.
- **FR-005**: The build container MUST have `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`,
  `--memory` and `--cpus` limits (flags `--sandbox-memory`, default `4g`; `--sandbox-cpus`, default `2`), and is
  removed when it exits. The sync timeout of 007 FR-014 applies (the container is killed on expiry).

### Test phase (no network, suite present, judge and impl separated)
- **FR-006**: pytest MUST run in a new container with `--network none`, a read-only root filesystem, `/tmp` as
  tmpfs, the copy at `/work` (read-write, owned by `impl`), the suite copy at `/suite` and an output directory at
  `/out`, both owned by `judge` with mode 0700, and the same limits as FR-005. The suite copy and `/out` live
  in a runner-private scratch directory, never inside the copy.
- **FR-007**: pytest MUST run as uid `judge`. The profile's launcher is used with syncing disabled
  (`uv run --no-sync`, `UV_OFFLINE=1`); `generic` uses the image's `python3 -m pytest`. `PYTHONPATH`, the plugin,
  records, junit and environment rules of 007 FR-012 apply with in-container paths (records and junit under
  `/out`).
- **FR-008**: The timeout of 007 FR-013 applies: on expiry the container is stopped (`docker stop -t 10`, then
  killed) and the status is `timeout`.
- **FR-009**: After the test container exits, the runner MUST copy `/out` to the private report directory, then
  remove the container, the suite copy, `/out` and the copy. Nothing from the test phase is written into the
  worktree or the `--repo` checkout.
- **FR-010**: Docker-dependent suites cannot run sandboxed: the test container has no Docker socket. A run whose
  `--pytest-arg` values select a `docker` marker (`-m docker` or an expression containing `docker` without `not`)
  MUST be refused with `E_SANDBOX_DOCKER` (exit 2).

### Separating the implementation from the judge
- **FR-011**: Inside the test container, `/suite`, `/out`, the records file and the plugin directory MUST NOT be
  readable or writable by uid `impl`.
- **FR-012**: The image MUST provide `/usr/local/bin/heldout-impl CMD [ARGS...]`: it runs `CMD` as uid `impl`
  (gid `impl`, no supplementary groups, empty ambient and bounding capability sets for the child), with the same
  stdin, stdout, stderr, working directory (or `/` when uid `impl` cannot enter it; amendment B1) and environment (minus `HELDOUT_RUN_RECORDS`), and exits with the
  child's status. Suites that launch the implementation as a process use it (the env var `HELDOUT_IMPL` holds its
  path in sandboxed runs and is unset otherwise, so a suite can write `[os.environ.get("HELDOUT_IMPL"), ...]`
  conditionally). The mechanism (a setuid helper, a root broker, `setpriv` with retained capabilities) is the
  implementer's choice; whatever capability it needs MUST be held only by that mechanism, never by pytest's
  process tree or the child.
- **FR-013**: Code imported into the judge's pytest process (a suite that imports implementation modules) runs as
  `judge`. The report MUST record `"isolation": "partial"` when any module whose file lies under `/work` was
  imported in the judge process at session end, else `"isolation": "full"`. This flag is informational: it is
  computed inside the process it describes.

### Tamper detection
- **FR-014**: Each sandboxed run MUST add two canary tests to the suite copy, with random names (>= 64 random bits
  each) and random file names: one that always fails and one that always passes, each marked
  `fr("CANARY")`. They are not counted under any id, never appear in the feedback, and are recorded in the private
  report.
- **FR-015**: The runner MUST also count the tests it expects from a collection-only pass run as `judge` in the test
  container before the main pass (`pytest --collect-only -q`, same arguments).
- **FR-016**: The status MUST be `tampered` (exit 1, round counted, feedback line `status: tampered` in place of the
  per-id lines: no ids, no counts) when any of: the failing canary is recorded as passed or missing; the passing
  canary is recorded as failed or missing; the number of recorded test results differs from the collected count;
  the junit file and the records disagree on any outcome.
- **FR-017**: The feedback text and JSON for `tampered` MUST contain only the feature, round, cap and status.

### Stated limits (replacing 007 FR-039 for sandboxed runs)
- **FR-018**: The module docstring and `--help` MUST state: sandboxed runs isolate the network, the host and the
  suite from the tree's processes and from anything that outlives the run; code imported into the judge process can
  still forge results within one run, which the canaries and count checks detect only partly; the counts are a
  low-bandwidth channel; Docker itself is the boundary (no claim against kernel or Docker escapes).

### Compatibility
- **FR-019**: Without `--sandbox`, every 007 behaviour, output and file is unchanged; the accepted suite
  `tests/heldout/007-heldout-runner/` MUST still pass, with one head amendment: its `ERROR_CODES` check accepts
  the 007 codes plus exactly the two codes this spec adds (amendment B1).
- **FR-020**: `scripts/heldout-run`, `scripts/run-heldout.sh` and the awork-resume vendoring rule of 007 FR-003 are
  unchanged (the new `heldout_run.py` is vendored by the head later, not in this feature).

## Success Criteria
- **SC-001**: For toy suites (all pass; one id failing; collection error; timeout), sandboxed and unsandboxed runs
  give identical feedback and exit codes.
- **SC-002**: The accepted suites `tests/heldout/005-multi-instance/` and `.../006-rsi-kernel-prerequisites/` run
  sandboxed with `--profile moeka` give the same per-id counts as unsandboxed runs on the same tree (excluding
  docker-marked tests).
- **SC-003**: A hostile toy tree (Story 2) leaves no file containing any suite text anywhere outside the runner's
  private report directory, and no network connection succeeds from the sync or test phase other than the sync
  step's registry access.
- **SC-004**: Each of at least five hostile toy trees that forge results in different ways (rewrite records,
  monkeypatch the plugin, mark everything passed via a pytest hook, exit early with status 0, delete the junit
  file) ends `tampered` or `collection_error`, never `passed`.
- **SC-005**: `heldout-impl` checks of Story 4 hold.
- **SC-006**: A sandboxed run of a small suite (under 20 tests, warm image) finishes within 60 s plus the
  unsandboxed time.

## Harness Contract (surfaces the hidden tests may use)
- The 007 contract (CLI, Python API, env vars, file locations), plus:
- CLI: `run ... [--sandbox none|docker] [--sandbox-image IMAGE] [--sandbox-memory M] [--sandbox-cpus N]
  [--sync-cmd CMD]`.
- Error codes added to `ERROR_CODES`: `E_SANDBOX_UNAVAILABLE`, `E_SANDBOX_DOCKER`. Status word added: `tampered`.
- Python API: constants `SANDBOX_DOCKERFILE` (the embedded text), `SANDBOX_JUDGE_UID = 10101`,
  `SANDBOX_IMPL_UID = 10102`.
- Private report `report.json` gains `meta.sandbox` (`"none"` or `"docker"`), `meta.image`, `meta.isolation`
  (FR-013) and `canaries` (names and outcomes).
- Env var visible to a sandboxed suite: `HELDOUT_IMPL=/usr/local/bin/heldout-impl`.
- Tests may run Docker (they are docker-marked themselves and run unsandboxed by the head).

## Assumptions
- Docker 24+ with BuildKit on the host; the head may pull public base images.
- Hostile-tree tests use toy trees and toy suites built in temp dirs; the GPU is never used.

## Amendment B1 (2026-10-01, head, from implementer questions)
- 007's accepted `test_error_codes_constant` is changed by the head to: the 007 set is a subset and the only extra
  codes are `E_SANDBOX_UNAVAILABLE` and `E_SANDBOX_DOCKER` (FR-019).
- `tampered` JSON keeps `schema` alongside feature, round, round cap and status; the text keeps the round-cap
  escalation line of 007. Neither carries test information (FR-017).
- FR-010: a `docker` term that is not directly negated (`not docker`) selects docker; `not slow and docker` is
  refused.
- FR-012: when `impl` cannot enter the caller's working directory, the command starts in `/`.
