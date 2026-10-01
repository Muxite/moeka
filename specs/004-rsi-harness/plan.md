# Implementation Plan: RSI Harness (pointer)

**Branch**: `004-rsi-harness` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)

## Summary

This plan only tracks the kernel-side work the harness needs and points to the harness's own milestones. The
harness repo (`moeka-rsi/`) does not exist; its design and implementation plan live on branch
`rsi-harness-spec` (`.agent/rsi-harness-design.md`, `.agent/rsi-harness-impl-plan.md`, `.agent/rsi-daemon-
feasibility.md`, `.agent/rsi-and-agent-core-report.md`). That branch is read, not edited, from here.

## Technical Context (as of 2026-09-30)

- Harness design status: draft, revised 2026-09-28 against the kernel API; nothing built (no repo, no task
  suite M3, no M1 Docker result). Milestones M1-M7: green core-slim; compose stack with supervisor; task suite
  v0 plus noise; baseline, archive, journal; tier-1 mutation loop; paper module; tier-2 decision.
- Pin: `core-slim` `6f80c392` (owner, 2026-09-30, closed). Kernel API unchanged between `5b9c7d43` and
  `6f80c392` per the harness design; its test suite differs.
- Kernel prerequisites, verified against code in this worktree:

  | Harness id | Requirement | State |
  |---|---|---|
  | K1 | skills in fingerprint | done (spec 006): `skills` component in `nanobot/kernel/variants.py` (`skills_component`) |
  | K2 | tool parameter-description overrides | done (spec 006): `Variant.tool_param_descriptions`, `VariantError`; applied per instance in `nanobot/agent/tools/loader.py` |
  | K3 | `args_digest` on `tool.call` | done (spec 006): `nanobot/kernel/trace_hook.py` `args_digest`, re-exported as `moeka.trace.args_digest` |
  | K4 | `max_tool_errors` enforced | done (spec 006): `ToolErrorCount` / `AgentRunSpec.max_tool_errors` in `nanobot/agent/runner.py`; stop reason `tool_error` |
  | K5 | strict sampling on agent runs | done (spec 006): `AgentSpec.on_unsupported`, `ModelSpec.unsupported_sampling`, pass-through fails closed under `raise` |
  | K6 | `skill.read` from any read | done (spec 006): `read_file` (after success) and `grep` content mode, with `via` and `call_id`; exec/MCP reads stay a stated lower bound |

- Clarification yield: no `clarify.*` trace event exists; `clarify.resolve_divergence` is pure.
- Usage and waste signals the harness wants depend on `001` (not built).
- The kernel's task-family baselines (`nanobot/kernel/baselines.py`) and the RSI baseline comparator are
  declared; the comparator that scores `E[Cost]` does not exist (design I6 status).

## Constitution Check

- VIII Verification Independence: harness gates use deterministic verification and held-out tests. PASS by
  design (not built).
- IX: the harness reads usage from `001`. GATE (depends on `001`).
- Mutation tiers: tier 1 only; core pinned read-only. PASS.
- X: harness references carry verified/unverified status (RSI design section 17). One entry ("The Blind
  Curator") is an unverified lead; locate and confirm before relying on it. GATE for any decision using it.

## Approach

- Kernel work lands in `core-slim` first, each as its own commit and test, then the harness bumps its pin.
- Order by first milestone needed (from the harness design): K3 and K1 (M4), K5 (M3), K2 and K4 (M5), K6
  optional. `001` waste and replay feed M4 onward.
- The harness repo and milestones are tracked on `rsi-harness-spec`, not duplicated here.

## Project Structure

```text
specs/004-rsi-harness/{spec,plan,tasks}.md   # this pointer only
(branch rsi-harness-spec) .agent/rsi-harness-design.md, rsi-harness-impl-plan.md
(future repo) moeka-rsi/                     # not created
```

**Structure Decision**: no code in this repo for the harness itself; only kernel prerequisites.

## Complexity Tracking

None. The pointer form avoids duplicating the harness design.
