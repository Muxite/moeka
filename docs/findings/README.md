# Findings: local register (moeka)

Open review items that belong to this repository, each with a status and an owner. Created by spec 010
(FR-005) on 2026-10-03. The cross-project register stays in awork-resume
(`/home/muk/projects/awork-resume/docs/findings/REGISTER.md`, entries tagged `moeka`); an item here that
also affects another project is mirrored there.

Rules:
- one row per item; the evidence column says what to run or read to re-check it;
- an item leaves the "Open" table only with a commit or an owner decision that closes it, recorded in
  "Closed";
- "owner" means muk (owner decisions) unless a spec or repository is named.

## Open

| Id | Item | Status | Owner | Evidence |
|---|---|---|---|---|
| R8 | CI cannot protect `main`: `NANOBOT_MIN_TESTS` floor is 5400 against about 10000 tests, strict `basedpyright` reports 791 errors (289 on the old `main`) | Open; waits on CLARIFY Q16 (review D-M5) | owner (decision), then whoever implements it in `.github/workflows/ci.yml` | `docs/reviews/2026-10-01-architecture-review.md` R8; `grep NANOBOT_MIN_TESTS .github/workflows/ci.yml` |
| R11 | The gateway still runs the legacy `AgentLoop.from_config` path with `LegacyEnvironment`: the state/work path overlap is allowed (a warning is logged), so invariant I2 does not hold for the live bot and I1 holds only through the adapter allow-list | Open, accepted for now; the fix is stage 2 of spec 003 (gateway onto `moeka.Kernel`) | owner (stage 2 decision), spec 003 | review R11; log line "overlap (legacy flat layout); allowed for now" |
| CLARIFY Q1 | Clarification yield: emit `clarify.question` / `clarify.commit_ready` and let the harness judge "needed" | Answered 2026-10-01 (option B), NOT implemented: no emitter exists in `nanobot/` or `moeka/` (grep 2026-10-03). Until then the objective is absent from Pareto comparisons | kernel (spec 004 FR-013, T010); the "needed" half is moeka-rsi's | `grep -rn "clarify.question" nanobot moeka` is empty |
| CLARIFY Q16 | CI as a gate: raise the test floor to about 9000 and scope the strict type check to the kernel packages | Unanswered | owner | `specs/CLARIFY-LOG.md` Q16 |
| Usage #13 | Forgeability of `llm_usage.sqlite3`: tamper EVIDENCE exists (`ProtectedFloor`, exec-floor hint, `kernel.usage.checkpoint()/verify()`), PREVENTION does not: an exec-capable agent can still edit the file | Partly fixed; SC-007 unproven. Prevention needs a writer in another process or uid | moeka-rsi (trust plane, its spec 004) for the harness; owner for the live gateway | `docs/reviews/2026-10-01-usage-schema-review.md` finding 13; `test_r11_*` |

## Accepted risks

- **Live gateway runs with `allowSudo=true` and `restrictToWorkspace=false`; containment is the RSI VM, not
  the ops agent.** The live bot is a host-management agent by design (`docs/PLAN.md`, "nanobot for server
  management"): it may run sudo and touch any path the service user can. Moeka's shell floor
  (`_FLOOR_DENY_PATTERNS`), deny patterns and exec screening are hints for honest mistakes, not a sandbox
  (`CLAUDE.md`, "Permissive shell sandbox"). Untrusted or self-modifying workloads (RSI rollouts, held-out
  suites) run only inside the moeka-rsi containment (VM plus hardened rollout container, moeka-rsi spec 002)
  or `heldout-run --sandbox docker` (spec 008), never in the live gateway. Owner: owner. Accepted
  2026-10-03 (spec 010 FR-005).

## Closed

| Id | Item | Closed by |
|---|---|---|
| Dream-bypass | Dream wrote four live skills that teach getting around moeka's own command allowlist (`background-du-bypass`, `bypass-command-allowlist`, `bypass-filter-piping`, `bypass-filter-resource-check`) | Spec 010: Dream skill writes are staged in `skills-pending/` by default (`dream.skill_writes`), workspace skills are linted at load (`capabilities.lint`, default `warn`), `nanobot capabilities lint` finds all four. The four live files themselves are the owner's to reject; this branch does not touch `~/.nanobot` |
| Usage #3 | Image generation, transcription and embedding calls were unmetered | Spec 010 FR-003: one `llm_usage` row per call (`purpose` image / transcription / embedding, `key_fp`); still not budget-gated |
| Arch R4 | Forgetting `host_tools=True` silently dropped `message`, `cron`, `run_cli_app` | Spec 010 FR-004: gateway, `serve` and CLI agent exit 2 at startup naming the missing tools |
