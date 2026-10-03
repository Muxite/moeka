# Tasks: library hygiene and audit fixes (spec 010)

- [x] T01 FR-002 `nanobot/capabilities/lint.py` (`lint_text`, `Finding`, four codes, `lint_file`)
- [x] T02 SC-001 fixture copies of the four live bypass skills; bundled skills lint clean
      (`tests/capabilities/test_lint.py`)
- [x] T03 FR-002 `CapabilitiesConfig.lint` (default `warn`); `SkillsLoader(lint_mode)` warn/refuse/off with a
      per-file cache; `ContextBuilder` and subagents apply it (`tests/agent/test_skills_lint.py`)
- [x] T04 FR-001 `DreamConfig.skill_writes` (default `pending`); `AgentLoop(dream_skill_writes)`;
      `MemoryStore.dream_skill_writes`
- [x] T05 FR-001 `nanobot/agent/dream_tools.py` pending mapping for write/edit/apply_patch/read;
      `build_dream_tools(skill_writes=)`; existing tests that assert `skills/` writes use `"direct"`
- [x] T06 FR-001 `dream.md` staged-writes bullet (absent in direct mode)
- [x] T07 SC-002 Dream run with a fake provider: `skills/` unchanged, one `skills-pending/` entry; merge edits a
      copy; direct mode writes `skills/` (`tests/agent/test_dream_pending.py`)
- [x] T08 FR-001 `nanobot/capabilities/pending.py` + CLI `pending`, `accept [--force-lint]`, `reject`
- [x] T09 FR-002 CLI `capabilities lint [PATH]`, `list` lint column (text and JSON)
      (`tests/capabilities/test_pending.py`)
- [x] T10 FR-003 `nanobot/llm_usage/side_calls.py` (`record_side_call`, fail-open, recorder override)
- [x] T11 FR-003 metered `ImageGenerationProvider.generate` (subclass wrapping, nested-call guard)
- [x] T12 FR-003 `TranscriptionProvider` base, metered `transcribe` on all adapters
- [x] T13 FR-003 metered `VecStore._embed` (local tier, cost 0); `usage_data_dir` through `MoekaCore`,
      `moeka.Kernel` memory registry and `DocStore`
- [x] T14 SC-003 one row each for image, transcription, embedding; failure never fails the call
      (`tests/llm_usage/test_side_calls.py`); R10 inventory test and `schemas/ATTRIBUTION.md` updated
- [x] T15 FR-004 `nanobot/cli/host_tools_guard.py`; guard in gateway, `serve`, CLI agent; `serve` checks
      no `cron` (`with_cron=False`); gateway-construction test (`tests/cli/test_host_tools_guard.py`); CLI fakes updated
- [x] T16 FR-005 `docs/PLAN.md` facts, spec 004 status and FR-008 pin, local findings register with the
      accepted-risk line, `CLAUDE.md`
- [x] T17 Import boundary probe extended (lint, pending, side_calls, dream_tools)
- [x] T18 Targeted Docker suites + `ruff check nanobot/`; full Docker suite
