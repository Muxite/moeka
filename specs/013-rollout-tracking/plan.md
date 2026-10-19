# Plan: rollout tracking across the kernel and the RSI harness (spec 013)

## Ownership

- **moeka (this repo)**:
  - schemas: `rollout-record.v1`, `capability-cert.v2`, the "Rollout tags" registry in `schemas/ATTRIBUTION.md`
  - verifying v2 certificates
  - the span-tag carry test
  - `001` T009 (memory and Dream attribution)
  - the held-out report fields (`scripts/heldout_run.py`)
- **moeka-rsi**:
  - consumer and tags at kernel construction, `rsi.ids`, the `rollouts` table
  - meter output in `usage-record.v1`, reconciliation
  - signal, author and judge payloads, capcert v2 issuing, the `rsi rollouts` CLI
  - restated by moeka-rsi `specs/006-rollout-tracking/spec.md` (pointer)
- **Order**: the moeka schemas and verifier land first. moeka-rsi then bumps its `core/` pin to a commit that has
  them and implements its side. No harness change depends on unreleased kernel code beyond that pin.

## Phases

1. **Attribution** (FR-001..005; cheap, highest value): consumer, session and tags in the harness; the tag
   registry; the carry test; T009. Done when SC-001 holds.
2. **Trusted usage** (FR-006..008, FR-013): meter records, the `rollouts` table, checkpoint and reconcile, the
   `rollout-record.v1` schema. Done when SC-002 holds.
3. **Evidence links** (FR-009..011, FR-014): signals list rollouts, capcert v2, judge payloads, the CLI. Done when
   SC-003 and SC-004 hold.
4. **Held-out binding** (FR-012): tree digest and commit in reports, `heldout-score` log entries. Done when SC-005
   holds.
5. **Deferred** (FR-016..018): tracked in `001`; they block turning the loop on by default, not this spec.

## Decisions

- **Reuse `rollout_id`, do not add a `run_id`.** It is already in `result.json`, the meter, the session key and
  `rollout_tags()`. A second name for the same thing is the defect this spec removes.
- **Session, not tags, carries the rollout.** `usage-record.v1` defines `session` as "a chat session key, a build
  id, a rollout id" and forbids `tags` from carrying it. The `rollout` tag stays because the meter and trace spans
  already use it; it must always equal the session's suffix (checked by SC-001).
- **The meter is the trusted source until `001` T022.** It runs outside the rollout's VM/containment and owns its
  file. The kernel ledger is evidence that must agree with it, never a source of truth. This follows
  `schemas/ATTRIBUTION.md` "Forging" and constitution IX.
- **Exact equality for `reconcile = ok`.** Both producers see the same upstream `usage` block, so any difference
  is a lost record, a forged row, or a call that bypassed the meter. Each of those is worth a flag. Cost
  differences are reported but do not decide the status, because the two sides price separately.
- **capcert v2 rather than widening v1.** `lineage` and `verifier` are `additionalProperties: false`. Adding keys
  would make v1 verifiers reject new certificates as `bad-schema`, which is a breaking change under the versioning
  rule. moeka accepts both versions, so certificates already issued stay valid.
- **`tree_sha256` reuses the bundle-hash construction of `012`** (canonical JSON of sorted
  `[relpath, sha256]`), so one helper (`nanobot.capabilities.provenance.bundle_sha256` semantics) defines code
  identity for both certificates and held-out reports.
- **The rollouts table lives in `supervisor.db`.** The supervisor already owns task state there, and a separate
  store would be a ninth place to look. The export schema is the stable surface; the table layout is internal to
  the harness.
- **The judge log stays authoritative for certificate events.** The rollouts table indexes them; it never
  replaces the signed chain (constitution VIII: the verifier's record stays separate from the producer's).

## Module layout

- moeka:
  - `schemas/rollout-record.v1.schema.json` and `schemas/capability-cert.v2.schema.json`, registered in
    `schemas/validate.py` (CLI names tuple), with examples in `schemas/examples/` and frozen copies plus tests in
    `tests/schemas/`
  - `nanobot/capabilities/provenance.py`: `cert_schema_problems` accepts v1 and v2, dispatching on `schema`
  - `tests/kernel/test_rollout_attribution.py`: span tags reach usage records (SC-001, kernel half)
  - `scripts/heldout_run.py`: `tree_sha256`, `commit`/`dirty` in worktree mode, `experiment_id`, report `1.1`
- moeka-rsi:
  - `src/rsi/ids.py` (FR-005)
  - `src/rsi/runner.py`: `consumer`, tags, checkpoint
  - `src/rsi/db.py`: `rollouts` table plus migration
  - `src/rsi/meter.py`: usage-record output
  - `src/rsi/reconcile.py`
  - `src/rsi/gaps.py`, `author.py`, `judge/provenance.py`, `promote.py`: payloads
  - `src/rsi/rolloutcli.py`: the `rsi rollouts` commands
