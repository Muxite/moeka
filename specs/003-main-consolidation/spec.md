# Feature Specification: Main Consolidation

**Feature Branch**: `003-main-consolidation`
**Created**: 2026-09-30
**Status**: Stage 1 executed 2026-10-01 on branch `consolidate/new-main` (kernel base + gateway host on top, tests green, smoke passed); cutover NOT performed (see `CUTOVER.md`); stage 2 (gateway onto `moeka.Kernel`) open.
Review 2026-10-01 (review/arch): findings and the stricter go/no-go gates G0-G7 (FR-012 to FR-016, SC-007 to
SC-009): `docs/reviews/2026-10-01-architecture-review.md`.
**Input**: Owner decision 2026-09-30: the kernel lives inside moeka `main`, and the gateway (channels, WebUI,
Telegram and Discord bot) sits on top as one consumer of it. Source: `.agent/moeka-kernel-design.md` section 12
("Branch consolidation") and section 14 risks; system contract section 9.

## Consumer Scenarios & Testing *(mandatory)*

### Consumer Story 1 - one branch serves the kernel and the live bot (Priority: P1)

moeka `main` contains the kernel (the `moeka` package and everything under it) and the full gateway; the live
bot runs from it; consumers pin `main`.

**Why this priority**: it is the owner's chosen end state; it ends the downstream-branch hazard where the
kernel and the bot drift apart.

**Independent Test**: in a worktree, the consolidated tree passes the kernel suite, the gateway suite and the
import-boundary test; the gateway starts in a dry run.

**Acceptance Scenarios**:

1. **Given** the consolidated tree, **When** the kernel suite runs, **Then** every kernel test that passes on
   `core-slim` passes.
2. **Given** the same tree, **When** the gateway suite runs, **Then** every gateway, channel, WebUI, cron and
   pairing test that passes on `main` passes.

---

### Consumer Story 2 - the gateway consumes the kernel, never the reverse (Priority: P1)

The gateway reaches providers, the agent loop and usage through the kernel; the kernel imports nothing from the
gateway.

**Why this priority**: keeps the embeddable kernel embeddable and I1-I6 provable.

**Independent Test**: the import-boundary test (kernel modules import no channel, WebUI, gateway, pairing,
audio, cron, triggers, apps, CLI or optional-features module) is green on the consolidated tree.

**Acceptance Scenarios**:

1. **Given** `import moeka`, **When** modules load, **Then** no gateway package is imported.
2. **Given** a gateway turn, **When** it runs, **Then** its calls appear in the usage surface (`001`).

---

### Consumer Story 3 - awork and the RSI harness move their pin once (Priority: P2)

awork and the harness change their pinned commit from `core-slim` `6f80c392` to `main` in one step, with no
rebase of any pinned branch and no broken pin in between.

**Why this priority**: both pin `core-slim` today.

**Independent Test**: each consumer's suite passes on the new pin before the old pin is retired.

**Acceptance Scenarios**:

1. **Given** the consolidated `main`, **When** awork pins it, **Then** awork's gate command passes.
2. **Given** the old pin, **When** the move happens, **Then** `core-slim` is unchanged and still resolvable.

---

### Consumer Story 4 - the live service is never disturbed (Priority: P1)

Building, testing and merging the consolidation never changes what the live service runs until the owner
chooses.

**Why this priority**: the service runs whatever is checked out in its working tree.

**Independent Test**: during the work, the live checkout's branch and HEAD are unchanged (recorded before and
after).

**Acceptance Scenarios**:

1. **Given** the work in a worktree, **When** it finishes, **Then** the live checkout is untouched.

### Edge Cases

- A file modified on both branches: no file is, as of 2026-09-30, changed on both sides since the merge base;
  if that changes, resolve per the rules below.
- A kernel-side edit to a shared file that removed a gateway hook (for example a bus or delivery call): the
  gateway needs it back behind a seam, not in the kernel.
- A plain merge of `core-slim` into `main`: wrong; it deletes the gateway.
- Config files shared between a `main` deployment and a slim tool (retired sections are dropped at load).
- The consolidated gateway still runs the legacy loop path through the legacy environment adapter, which allows
  the work and state areas to overlap: invariants I1-I6 are proven for kernel hosts, not for the gateway, until
  FR-009 stage 2. Docs MUST NOT claim otherwise.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: the gateway, channels, WebUI, cron, pairing, audio, triggers, apps, the `message` tool and the
  gateway's CLI commands MUST be preserved from `main`.
- **FR-002**: the kernel (`nanobot/kernel/`, the `moeka` package, the facade and document-store modules, the
  usage store) and the `.agent/` design and spec docs MUST come from `core-slim`.
- **FR-003**: kernel modules MUST NOT import gateway, channel, WebUI, pairing, audio, cron, triggers, apps, CLI
  or optional-feature modules; the import-boundary test MUST stay green and gain `moeka` coverage.
- **FR-004**: constitution principles I-VI MUST hold on the consolidated tree. The ambient-read guard MUST keep
  failing on a forbidden read in kernel packages; any host-side package that legitimately reads the process
  environment MUST be named in an explicit allow-list with a reason, not silently exempted.
- **FR-005**: files modified on both branches MUST be resolved by rule: kernel-owned paths take `core-slim`;
  gateway-owned paths take `main`; a shared file is merged by hand with the kernel behaviour preserved and the
  gateway hooks restored through a seam recorded in the plan.
- **FR-006**: moeka-specific deviations documented in `CLAUDE.md` MUST survive (permissive shell floor,
  sqlite session store, lazy config rebuild, flat layout, `${VAR}` warnings) and the two `CLAUDE.md` variants
  MUST be reconciled into one.
- **FR-007**: `core-slim` MUST NOT be rebased, force-pushed or renamed by the work; the consolidation lands as
  new commits on a new branch from `main`; consumer pins stay valid until each consumer migrates.
- **FR-008**: the live checkout (`~/projects/moeka`) MUST NOT be used for the work; all steps run in worktrees.
- **FR-009**: the gateway MUST move onto the public kernel API in defined steps, each independently testable,
  with the legacy loop path removed only after the gateway uses the kernel. [NEEDS CLARIFICATION: how the
  gateway moves onto `moeka.Kernel` (today it uses the legacy loop path and passes no plugin registry) and the
  order of steps; executed as option A: consolidate first with the gateway unchanged (done, 2026-10-01), then migrate it to the
  kernel API behind a switch (stage 2, open; owner to confirm) (CLARIFY-LOG Q8)]
- **FR-010**: after consolidation, `nanobot.api` on `main` MUST keep the HTTP API server while the legacy
  completion module is removed under `002-kernel-api-for-consumers`.
- **FR-011**: a consolidation record MUST list, for each top-level path, which branch it came from and why,
  so a reviewer can audit the selection.
- **FR-012**: before the live checkout or service changes, a verified backup of live state MUST exist on two
  physically separate disks: the session database (made with the SQLite backup API, not a file copy), the memory
  files, the cron store, the config and the usage database. Verified means each copy opens, passes an integrity
  check and has the same session and message counts as the live file. An offline or unmounted backup tier does
  not count.
- **FR-013**: before cutover, a parity check MUST compare the old and the candidate tree on the live config: the
  config validates, the registered tool set (differences listed and approved), the exec child-environment keys,
  the channel plugin list and the provider request. Any unlisted difference blocks the cutover.
- **FR-014**: a canary on a throwaway chat-channel credential and a copy of the workspace MUST handle real turns
  (one needing history, one tool call, one scheduled heartbeat) before the live service is switched. The live
  credential is never used by two processes.
- **FR-015**: the cutover MUST NOT run a dependency sync when the lock file is unchanged; if one is run, the
  channel runtime dependencies are re-enabled and import-checked before the service starts.
- **FR-016**: publishing the consolidated `main` to a public remote MUST wait for a soak period after cutover and
  an owner approval; a rollback after a public push needs a force push and is therefore not part of the rollback
  plan.

### Key Entities

- **Consolidated main**: the integration result.
- **Kernel-owned path / gateway-owned path / shared path**: the three ownership classes used by FR-005.
- **Pin**: a consumer's recorded commit of moeka.
- **Seam**: a small interface through which the gateway uses the kernel.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: kernel suite and gateway suite are each at least as green on the consolidated tree as on their
  source branches (0 newly failing tests, with any intentional drop listed).
- **SC-002**: import-boundary and ambient-read guards are green; the allow-list diff is reviewed and each added
  entry has a reason.
- **SC-003**: the live checkout's branch and HEAD are identical before and after the work.
- **SC-004**: both pinned branches resolve to their original commits after the work (0 rewritten commits).
- **SC-005**: awork and the harness suites pass on the new pin.
- **SC-006**: every top-level path has an entry in the consolidation record.
- **SC-007**: two verified backup copies of the session database exist on different disks before the first
  change to the live checkout (FR-012).
- **SC-008**: the parity check reports 0 unapproved differences (FR-013); the only approved tool-set addition is
  recorded by name.
- **SC-009**: the rollback is rehearsed on a copy and completes in under 5 minutes before the cutover.

## Assumptions

- The owner may veto any step; nothing here executes without a separate instruction.
- `core-slim` stays as a historical, pinned branch until consumers move.
- Upstream (`HKUDS/nanobot`) syncs continue to land on `main` after consolidation.
- The consolidation happens after, or independently of, `001` and `002`; none blocks it.
