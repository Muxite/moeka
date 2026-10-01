---
description: "Task list for 007-heldout-runner"
---

# Tasks: Held-out Runner (`heldout-run`)

**Input**: `specs/007-heldout-runner/spec.md`, `plan.md`. Paths as of 2026-10-01.

**Prerequisites**: implementer works in its own moeka worktree cut from `consolidate/new-main`. Held-out tests live
in `~/projects/.heldout/moeka/007-heldout-runner/`; the implementer MUST NOT read that directory. Until this runner
exists, the head runs that suite by hand (005 RUN.md procedure) and relays ids and counts only.

**Tests**: test-first. The implementer's own tests go in `tests/scripts/test_heldout_run.py` and build fixture repos
and suites in temp dirs (`HOME`, `HELDOUT_ROOT`, `TMPDIR` all temp; `--profile generic`).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency)
- **[Story]**: US1 run and feedback, US2 redaction and isolation, US3 triage, US4 rounds

## Phase 1: Setup

- [ ] T001 Tester: hidden suite from spec.md only, under `~/projects/.heldout/moeka/007-heldout-runner/` (mode
  0700), every test `@pytest.mark.fr(...)`, `SPEC-MAP.md`, `RUN.md`; includes the canary suite for SC-002.
- [x] T002 [P] Fixture builders in `tests/scripts/conftest.py` (or a helper module): temp git repo with a tiny
  package, suite factory (markers, `SPEC-MAP.json`, `fr_report.py` `MAP`), canary-token generator.

## Phase 2: Foundational

- [x] T003 Skeleton `scripts/heldout_run.py`: `main(argv)`, error type with `E_*` codes, no-echo argparse,
  `--version` with own sha256 (FR-001, FR-002, FR-025, FR-026); wrapper `scripts/heldout-run`.
- [x] T004 AST test that module-level imports are stdlib only and nothing imports `moeka`/`nanobot`/`awr` (FR-001).

## Phase 3: User Story 1 - run and feedback (P1)

- [x] T005 [US1] Tree copy: `--repo/--ref` via `git archive`; `--worktree` filtered copy; no `.git`; scratch
  placement (FR-006 to FR-008).
- [x] T006 [US1] Suite placement into `<copy>/tests/heldout/<feature>/`, suite untouched (FR-009); cleanup on every
  exit path and signal (FR-010).
- [x] T007 [US1] Profiles and pytest invocation, child environment, plugin injection (FR-011, FR-012).
- [x] T008 [US1] Plugin hooks and records; outcome rules; timeout with process-group kill; collection and sync
  errors (FR-013 to FR-015, FR-020).
- [x] T009 [US1] Id mapping: markers, `SPEC-MAP.json`, `fr_report.py` via `ast.literal_eval`, `INVALID_ID`,
  `UNMAPPED` (FR-017 to FR-019).
- [x] T010 [US1] `summarize`, `format_feedback`, `--json`, `--feedback-file`, ordering rules (FR-021 to FR-023).
- [x] T011 [US1] Private report layout and modes (FR-027, FR-028); feature lock (FR-016).

## Phase 4: User Story 2 - redaction and isolation (P1)

- [x] T012 [US2] Placement checks against the tree, `git worktree list`, `--forbid-under`; permission check;
  `check-isolation` (FR-005, FR-029 to FR-031).
- [x] T013 [US2] Redaction tests over every status and refusal with canary tokens and an absolute-path regex
  (FR-024, SC-002); source tree and suite unchanged, scratch removed (SC-003).
- [x] T014 [P] [US2] Stated limit in the module docstring and `--help` (FR-039). Pending owner answer on sandboxing;
  default: no sandbox.

## Phase 5: User Story 4 - rounds (P2)

- [x] T015 [US4] `rounds.json`, counting rules, cap refusal, escalation line, `rounds` and `--reset --reason`
  (FR-032 to FR-035). Pending owner answer on tester-bug rounds; default: counts unless reset.

## Phase 6: User Story 3 - triage (P2)

- [x] T016 [US3] `triage`: failure message extraction, `extract_requirement` grammar, banner, `--json` (FR-036 to
  FR-038, SC-007).

## Phase 7: Adoption in both repos

- [ ] T017 Parity check (SC-005): run moeka `005-multi-instance` and awork-resume `007-data-retention` accepted
  suites through `heldout-run` on their accepted refs; compare per-id totals with their own summaries. Record the
  numbers in this file.
  - 2026-10-01 awork-resume `007-data-retention` at `7af9ea8` (profile `awork-resume`, `--no-count`): status
    passed, 194 tests, 68 ids, 0 failing; per-id totals from `report.json` equal `fr_report.py` on the same junit
    (0 unmapped). moeka `005-multi-instance`: not run yet (host run not permitted in the implementer session).
- [ ] T018 awork-resume (its own branch and commit, owner-approved): vendor `scripts/heldout_run.py` and
  `scripts/heldout-run` byte-identical, add `scripts/heldout_run.SOURCE` with the moeka commit (on `origin`), and a
  test that the file's sha256 matches the moeka file at that commit when available (FR-003). Do not change
  `scripts/run-heldout.sh` (FR-004).
- [ ] T019 Replace the manual "run and redact" steps in held-out `RUN.md` templates with `heldout-run run ...`;
  update `004` tasks T013-T015 (T015 answered by this runner; no kernel helper) and the plan step M10 status.
- [ ] T020 Head: run the hidden 007 suite by hand (005 RUN.md procedure), never with the runner under test (it is
  the implementer's code, FR-005); triage, round cap 4; on acceptance commit it to `tests/heldout/007-heldout-runner/`
  and check `scripts/run-heldout.sh 007-heldout-runner`.

## Dependencies

- T003-T004 before everything; T005-T011 before T012-T016; T017 after T016; T018 after the moeka commit is pushed
  by the owner (pins resolve from origin); T019-T020 last.
- Spec 006's acceptance (006 T018) can use this runner once T017 passes.
