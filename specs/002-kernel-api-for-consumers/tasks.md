---
description: "Task list for kernel API for consumers"
---

# Tasks: Kernel API for Consumers

**Input**: `specs/002-kernel-api-for-consumers/spec.md`, `plan.md`
**Tests**: required. Paths as of 2026-09-30. Work happens in a worktree, never the live checkout.

## Phase 1: Setup

- [ ] T001 Write the requirement-to-row traceability of every deprecated entry point against `docs/migration-moeka-api.md`

## Phase 2: Foundational

- [ ] T002 Decide CLARIFY-LOG Q4 (stop contract); record in `spec.md`
- [ ] T003 [P] Doc-versus-code drift test `tests/kernel/test_migration_doc_names.py`: every `kernel.*`, `moeka.*`, `AgentSpec.*` name in the page resolves (FR-012, SC-006)

## Phase 3: Consumer Story 1 - awork migrates (P1)

- [ ] T004 [US1] Add a "Stop contract" section to `docs/migration-moeka-api.md` and `docs/python-sdk.md`, and a test `tests/kernel/test_stop_contract.py` covering exception, `BatchResult.systemic` and `stop_reason="budget"` (FR-008)
- [ ] T005 [P] [US1] Decide and implement the `from_profile` mapping for `planning`/`limits` or keep the documented `ValueError` in `nanobot/kernel/agent.py` (FR-005)
- [ ] T006 [P] [US1] Add a collection enumerate/export method to `DocStore` in `nanobot/kernel/memory.py` with test `tests/kernel/test_memory.py` (FR-006)
- [ ] T007 [US1] Provide a research-agent example (allow-list including `search_documents`) in `docs/migration-moeka-api.md`; verify with a test using the fake provider
- [ ] T008 [US1] Declare any dependency the public API imports in `pyproject.toml` and `uv.lock` (`rapidfuzz` is present; verify no other is missing) (FR-014)
- [ ] T009 [US1] awork side (outside this repo): migrate `backend/awork/moeka_compat.py`, add `rapidfuzz` to awork's lock, run the awork gate command
- [ ] T010 [US1] Usage agreement test: legacy `usage_sink` figures equal `model.call` figures for the same calls in `tests/api/test_usage_agreement.py` (FR-015, depends on `001` T010)

## Phase 4: Consumer Story 2 - embedder (P2)

- [ ] T011 [P] [US2] Fresh-venv script test using only `moeka` imports, asserting nothing under HOME, in `tests/kernel/test_embedder_smoke.py` (SC-003)
- [ ] T012 [P] [US2] Extend `tests/core/test_import_boundary.py` to cover `import moeka` (FR-004)

## Phase 5: Consumer Story 4 - harness (P2)

- [ ] T013 [P] [US4] Two-kernel isolation test (variants, registries) in `tests/kernel/test_variants.py` if absent (SC acceptance 1)

## Phase 6: Removal (after T009 is merged and awork pins the migrated commit)

- [ ] T014 Replace legacy modules with import-time errors naming replacements: `nanobot/api/complete.py`, `nanobot/core/core.py`, `nanobot/core/vec.py` (FR-013)
- [ ] T015 Delete the legacy tests and `nanobot/_deprecation.py` once no shim remains; keep `tests/core/test_import_boundary.py`
- [ ] T016 Update `CLAUDE.md` (its architecture list still names `MoekaKernel` as the embedding surface) and `.agent/moeka-kernel-design.md` naming note
- [ ] T017 On `main` after 003: keep `nanobot/api/{runtime,server}.py`; remove only `complete.py`

## Dependencies

- T002 precedes T004. T009 blocks Phase 6. T010 depends on spec `001` T010 and T011.
