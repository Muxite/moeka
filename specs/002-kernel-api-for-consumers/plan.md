# Implementation Plan: Kernel API for Consumers

**Branch**: `002-kernel-api-for-consumers` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)

## Summary

The public API (`moeka`) exists on `core-slim`; this plan finishes the consumer side: migrate awork, close the
friction the migration page records, define the stop contract, then delete the legacy shims.

## Technical Context (as of 2026-09-30; `core-slim` at `2438fffe`; tests read, not re-run)

- `moeka/` re-exports `nanobot/kernel/`; `moeka/__init__.py` exports `CredentialResolver, Environment,
  Kernel, ModelSpec, Paths, PathsOverlapError, ProviderSpec, Sampling, StaticCredentialResolver`.
- Legacy entry points still present and deprecated through `nanobot/_deprecation.py::warn_deprecated`:
  `nanobot/api/complete.py` (`acomplete`, `acomplete_stream`, `complete_stream`, `acomplete_json`,
  `complete_json`, `complete`), `nanobot/core/core.py` (`MoekaCore`/`MoekaKernel`), `nanobot/core/vec.py`
  (`open_vec_store`). The legacy `usage_sink` is `_report_usage` in `nanobot/api/complete.py`.
- `main` also has `nanobot/api/{runtime,server}.py` (HTTP API server); `core-slim` keeps only `complete.py`.
  Removal on `main` must not delete the server (see 003).
- Stop contract today: `BudgetExceeded` is an `LLMError` exported by `moeka.errors`; batch sets
  `BatchResult.systemic`; agent runs return `stop_reason="budget"`. `BudgetHalt` is awork's
  (`awork/llm.py`), not in moeka.
- Friction recorded in `docs/migration-moeka-api.md` and checked in code: `AgentSpec.from_profile` raises
  `ValueError` for `planning` or `limits` (`nanobot/kernel/agent.py:336`); `DocStore` has `count`, `sources`,
  `clear`, `search` but no enumerate-all (`nanobot/kernel/memory.py`); awork's research profile allow-list (`RESEARCH_TOOLS`) lacks
  `search_documents`.
- awork consumes via `backend/awork/moeka_compat.py` on its unmerged branch `compat/moeka-core-slim`, pinning
  `6f80c392`. `rapidfuzz` is a `core-slim` dependency absent from awork's lock (design section 14). Not re-run.
- The public-API plan referenced as "Tasks 1-15" in the design is not present in this worktree; its Task 14
  (awork migration) and Task 15 (shim removal) are not done.

## Constitution Check

- I (zero ambient reads): `Environment.for_host` and `from_config` read nothing ambient; legacy shims go
  through `LegacyEnvironment`. PASS for the target; legacy removal strengthens it.
- IX: usage through `001`. GATE for FR-015.
- Compatibility rule: no removal before awork pins. GATE for FR-013.
- V: `RunLimits.max_tool_errors` raises `NotImplementedError` (declared, not enforced), which honours the
  "must raise" clause. PASS.

## Approach

1. Add the stop-contract page and test (FR-008) after CLARIFY-LOG Q4.
2. Close migration friction: `from_profile` mapping or documented error, enumerate/export on `DocStore`,
   doc-versus-code drift test.
3. awork migrates on its branch (outside this repo); moeka provides a compat checklist and the awork gate
   command from the design.
4. After awork pins a migrated commit: delete `nanobot/api/complete.py`, `MoekaCore` facade and
   `open_vec_store`, replace with import-time errors naming replacements for one release, then delete.

## Project Structure

```text
specs/002-kernel-api-for-consumers/{spec,plan,tasks}.md
moeka/                         # public surface
nanobot/api/complete.py        # legacy, to remove
nanobot/core/                  # legacy facade, to remove (vec.py, core.py)
docs/migration-moeka-api.md    # keep accurate; add stop-contract page
docs/python-sdk.md
tests/api/ tests/core/ tests/kernel/
```

**Structure Decision**: unchanged single library; removal is deletion plus a guard test.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Keeping shims until awork pins | compatibility rule | early deletion would break awork at call time (its shim filters the warning) |
