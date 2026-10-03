# Feature Specification: Library hygiene and audit fixes

**Feature Branch**: `feat/010-hygiene` (base: `integrate/009-011`)
**Created**: 2026-10-03
**Status**: Draft (implementation-ready; held-out pattern)
**Input**: owner plan Phase 2 and the 2026-10-03 gap audit:
- Dream wrote four live skills that teach bypassing moeka's own command allowlist;
- workspace skills are never validated;
- image generation, transcription and embedding calls are unmetered (usage review 2026-10-01 #3);
- `host_tools` silently drops host tools when forgotten (architecture review R4);
- several docs are stale.

## Requirements

- **FR-001 Pending skills.** Config `dream.skill_writes: "pending" | "direct"` (default `"pending"`). With
  `"pending"`:
  - Dream's skill create and merge writes go to `<workspace>/skills-pending/<name>/` instead of
    `<workspace>/skills/<name>/`. The Dream prompt (`nanobot/templates/agent/dream.md`) says so, and the
    filesystem tools Dream uses map the target. Dream may still read `skills/`;
  - `SkillsLoader` never lists or loads `skills-pending/`;
  - `nanobot capabilities pending` lists pending skills with validation and lint results;
  - `nanobot capabilities accept <name>` moves one into `skills/` only if `validate_skill` (spec 011) returns no
    problems and lint (FR-002) reports none, unless `--force-lint`; it exits 1 otherwise;
  - `nanobot capabilities reject <name>` moves it to `<workspace>/skills-rejected/<date>/`.
  With `"direct"`, writes go to `skills/` as today.
- **FR-002 Lint.** `nanobot/capabilities/lint.py`: `lint_text(text) -> list[Finding(code, line, excerpt)]`.
  Finding codes:
  - `bypass-language`: case-insensitive match of `bypass`, `circumvent` or `evade` within 40 characters of
    `allowlist`, `denylist`, `deny pattern`, `filter`, `sandbox`, `guard`, `restriction` or `permission`;
  - `pipe-to-shell`: `curl|wget … | (sh|bash)` or `base64 -d … | (sh|bash)`;
  - `sudo-stdin`: `sudo -S`;
  - `secret-path`: `keys.env`, `.ssh/id_`, `auth.json`.
  `capabilities.lint: "off" | "warn" | "refuse"` (default `"warn"`). Each mode applies to workspace skills at load:
  - `warn` logs once per skill and finding code, and `nanobot capabilities list` shows `lint:<codes>`;
  - `refuse` skips the skill.
  `nanobot capabilities lint [PATH]` prints findings and exits 1 if any are found.
- **FR-003 Metered side calls.** Image generation (`nanobot/providers/image_generation.py`), transcription
  (`nanobot/providers/transcription.py`) and `VecStore` embedding calls produce usage records in the same
  `llm_usage` store:
  - `kind`/`purpose` respectively `image`, `transcription` and `embedding`, with provider, model and key_fp
    (spec 009);
  - tokens or units when known; local embedding models have tier `local` and cost 0;
  - a recording failure never fails the call.
- **FR-004 Host tools guard.** `AgentLoop.from_config(..., host_tools=False)` keeps working for kernel hosts. The
  gateway and CLI agent entry points assert after construction that `message`, `cron` and `run_cli_app` are
  registered; `serve` wires no cron service, so it asserts only `message` and `run_cli_app` and must start without
  `cron`. A failed guard ends startup with exit 2 and a message naming the missing tools. A test builds the
  gateway loop as the gateway does and checks the three tools.
- **FR-005 Docs.**
  - Bring `docs/PLAN.md`'s repository facts up to date (Dockerfile and compose exist).
  - Set `specs/004-rsi-harness/spec.md` Status to say what is built (the containment, held-out runner and
    sandbox specs) and where the harness lives now (moeka-rsi specs 001-005), and correct the FR-008 pin.
  - Turn `docs/findings/README.md` into a local register listing the open review items (R8 CI on main, R11 path
    overlap, CLARIFY Q1, Q16, usage review #13), each with status and owner. Record the accepted risk "live
    gateway runs with `allowSudo=true` and `restrictToWorkspace=false`; containment is the RSI VM, not the ops
    agent".

## Test Harness Contract

`nanobot.capabilities.lint.lint_text`, `Finding`; `SkillsLoader` over a temp workspace; Dream run with a fake
provider as existing Dream tests do (`tests/agent/test_dream*.py`); the `llm_usage` store with a temp data dir and
fake image/transcription/embedding backends; CLI via `python -m nanobot capabilities ...`; gateway loop construction
as `nanobot/cli/gateway_runtime.py` does it.

## Success Criteria

- **SC-001** The four live bypass skills' text (fixture copies) each produce ≥ 1 `bypass-language` finding, and
  every bundled skill under `nanobot/skills/` produces none.
- **SC-002** A Dream run that creates a skill with default config leaves `skills/` unchanged and adds one entry to
  `skills-pending/`.
- **SC-003** Image, transcription and embedding calls each add exactly one usage row.
