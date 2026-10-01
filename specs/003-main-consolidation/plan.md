# Implementation Plan: Main Consolidation

**Branch**: `003-main-consolidation` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)

## Summary

Produce a new branch from `main`, bring the kernel and design docs from `core-slim`, reconcile shared files by
hand, keep the gateway untouched at first, then migrate the gateway to the kernel API in a second stage. Nothing
is executed by this plan.

## Technical Context (as of 2026-09-30; verified with git in this worktree)

- Branches: `main` (full gateway, live bot) and `core-slim` (slim kernel). Merge base `b1530a0c`. Since the
  base: `main` has 1 commit (`54069bb2`, a deploy-runbook doc change); `core-slim` has 164. Files changed on
  both sides since the base: 0.
- `git diff --name-status main core-slim`: 1332 files, of which 179 added on `core-slim`, 951 deleted, 201
  modified. Deleted groups: `nanobot/channels` (343), `webui/src` (312), `nanobot/webui` (49), `tui/src`
  (48), plus `nanobot/{cron,triggers,apps,audio,gateway}`, 10 under `nanobot/cli`, 4 `nanobot/templates`. Added:
  `nanobot/kernel` (35), `nanobot/agent` (25), `nanobot/security` (3), `tests/kernel` (49), `docs/core-map`.
- The 201 modified files include the core of the shared runtime: `nanobot/agent/{loop,runner,subagent,context,
  memory,hook}.py`, `nanobot/agent/tools/*`, `nanobot/providers/*`, `nanobot/config/*`, `nanobot/core/*`,
  `nanobot/llm_usage/*`, `nanobot/cli/*`, `nanobot/command/builtin.py`, `nanobot/nanobot.py`. Each needs review
  for gateway coupling (for example `nanobot/agent/turn_delivery.py`, `nanobot/bus`).
- `main` has `nanobot/api/{complete,runtime,server}.py`; `core-slim` keeps only `complete.py` and `__init__.py`.
- `main` has `nanobot/core/core.py` but no `nanobot/kernel/` and no `moeka` package.
- Ambient-read guard: `tests/kernel/test_no_ambient_reads.py` allow-lists `nanobot/cli/`, `nanobot/config/`,
  `nanobot/kernel/legacy.py`, `nanobot/utils/restart.py` (plus the `nanobot/utils/path.py` exemption). A
  grep on `main` finds ambient-read patterns (`os.environ`, `os.getenv`, `Path.home()`, `expanduser`) in
  `nanobot/channels` (22), `webui` (21), `apps` (16), `gateway` (6), `audio` (2); `expanduser` on a caller path
  is allowed, so some are benign. The guard would need a reviewed host-side allow-list for these.
- Import boundary: `tests/core/test_import_boundary.py` forbids the kernel's import of `nanobot.channels`,
  `.web`, `.webui`, `.gateway`, `.pairing`, `.audio`, `.cron`, `.triggers`, `.apps`, `.cli`,
  `.optional_features`.
- The live service: `moeka.service` has `WorkingDirectory=%h/projects/moeka` and runs `bin/moeka.sh run`. The
  live checkout is not touched by this work.
- Pins: awork's unmerged `compat/moeka-core-slim` and the RSI harness pin `core-slim` `6f80c392`, which is on
  `origin/core-slim`; the docs commits after it are local.

## Constitution Check

- I-VI hold only if the guard and boundary tests run on the consolidated tree. GATE (FR-003, FR-004).
- Governance: no rebase while pins exist. GATE (FR-007).
- Live service protection (constitution, constraints). GATE (FR-008).

## Approach (proposal, subject to CLARIFY-LOG Q8)

1. Record the baseline: live checkout branch and HEAD; both suites' results on `main` and `core-slim`.
2. New branch `consolidate/kernel-on-main` from `main`, in a new worktree.
3. Bring kernel-owned paths with `git checkout core-slim -- <paths>`: `nanobot/kernel`, `moeka`,
   `nanobot/llm_usage`, `nanobot/security`, kernel tests, `docs/core-map`, `.agent/` design docs.
4. For each of the 201 shared files, take the `core-slim` version as the starting point, then restore
   gateway hooks that `core-slim` removed, behind a seam; list each decision in the consolidation record
   (FR-011). Alternative to evaluate: start from `core-slim` and restore the deleted gateway directories from
   `main`; choose the direction with fewer manual hunks, measured by a dry run.
5. Extend the boundary test to `moeka`; add the reviewed host-side allow-list to the ambient guard.
6. Run both suites in a worktree via `scripts/test-docker.sh`; dry-run the gateway.
7. Stage 2 (separate spec tasks): gateway onto `moeka.Kernel` behind a switch, then delete the legacy path.
8. Move consumer pins one at a time; retire nothing until both pass.

## Project Structure

```text
specs/003-main-consolidation/{spec,plan,tasks}.md
docs/consolidation-record.md      # per top-level path: source branch and reason (created during execution)
```

**Structure Decision**: documentation only now; execution adds the record file and the integration branch.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Hand merge of shared files | 201 shared files were edited for the slim build | a plain merge deletes the gateway; a file-level take loses one side |
| Host-side allow-list for ambient reads | gateway packages read process state | exempting silently would void I1 |

## Execution result (2026-10-01)

Direction as decided by the owner: `core-slim` is the base, the gateway comes from `main`. Branch
`consolidate/new-main` (worktree `/home/muk/projects/moeka-newmain`); `main` is merged in with `-s ours`, so it
fast-forwards. Per-path record: `docs/consolidation-record.md`. Cutover steps: `CUTOVER.md`.

### Seams as built

- `AgentLoop(host_tools=True)` / `ToolContext.host_tools`: `message`, `cron`, `run_cli_app` register only for host
  loops (gateway, `serve`, CLI agent). A bare loop and every `moeka.Kernel` agent keep the kernel tool set
  (`tests/agent/test_registered_tool_names.py`). `tool_contract.md` renders host sections from the registered tool names.
- Host imports are lazy on the kernel path: automation coordinators in `AgentLoop.__init__`, WebUI helpers in
  `session/recovery.py` and `utils/restart.py`, `CronSchedule` in `config/schema.py`, `nanobot.cron`/`nanobot.apps`
  in the `cron` and `cli_apps` tool modules, CLI-app skill aliases in `agent/skills.py` (skipped for a strict env).
- Config: the `channels`, `transcription`, `api`, `gateway` (+ `heartbeat`), `tools.cliApps`,
  `webuiAllowRemotePackageInstall` sections and `DreamConfig` scheduling are back; the slim "retired section" strip in
  `_migrate_config` is gone.
- Ambient reads: `HOST_AMBIENT_ALLOWLIST` (reasons inline). The WebUI now passes `os.environ` to the sandbox helpers,
  since the security layer no longer reads the process environment.
- `conftest.py` keeps the kernel session-root isolation and restores the pairing-store isolation fixture.

### Stage 2 remaining (gateway onto `moeka.Kernel`, FR-009, T014)

- The gateway builds `AgentLoop` through `AgentLoop.from_config` with a `LegacyEnvironment`, with no plugin
  registry, policy gate or kernel `Budget`; `nanobot/api/server.py` and `runtime.py` use the same path.
- `/dream` (`command/builtin.py`) and the gateway's scheduled Dream job (`cli/gateway_runtime.py`) keep their own
  copy of the Dream run; the kernel has `nanobot/agent/dream.py:run_dream`.
- Host slash commands (`/trigger`, `/pairing`, `/evaluator-prompt`) register on every loop; gate them behind `host_tools`.
- `search_sessions`/`read_session` use the kernel's `_SessionAccess`, not the WebUI's `WebuiSessionAccess`.
- `templates/AGENTS.md` (workspace seed) is the kernel one: no reminder/heartbeat section for new workspaces.
- Duplicate `notification_metadata` (kernel `turn_delivery.py` vs `channels/notification_routes.py`).
- `Nanobot` SDK facade loads no host tools; `AgentLoop._automation_specs()` loads cron/trigger specs lazily at run time.
- `basedpyright` strict: 791 errors versus 289 on old `main`.
