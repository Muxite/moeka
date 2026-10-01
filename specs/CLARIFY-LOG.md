# Clarification Log

Owner questions, in spec-kit `/clarify` style: each has the question, the options, a recommended answer with its
reason, and where the answer lands. Status is Open until the owner answers; then the answer is encoded into the
owning spec and the entry gets a dated "Answered" line. Created 2026-09-30.

Q1-Q4 and Q8 are the open owner decisions that carry a `[NEEDS CLARIFICATION]` marker in a spec. Q5-Q7 and Q9-Q13
carry recommended defaults and no marker (the design already records a default, or the choice is mechanical);
the owner may veto any of them.

## Questions with a marker

### Q1. Clarification yield: emit a trace event, or drop the objective?
- Spec: `004-rsi-harness` FR-013. Source: kernel design sections 8 and 12.
- Context: `clarify.resolve_divergence` and `Epistemics.reconcile` are pure and emit no event, so questions
  asked are not counted. The "needed or not" half needs a downstream outcome that no event carries.
- Options:

  | Option | Effect |
  |---|---|
  | A. Emit `clarify.question` and `clarify.commit_ready` (paths and reasons, never values), plus a way to mark a question needed or not | objective becomes measurable; more kernel surface |
  | B. Emit the events only, and compute "needed" in the harness from outcomes | kernel stays small; harness owns the downstream half |
  | C. Drop clarification yield from the Pareto objectives until the harness can measure it | no work now; the objective stays absent |

- Recommended: B (events now, in the kernel; the "needed" judgement stays in the harness). Reason: the event is
  cheap and also serves audit; the outcome half cannot live in the kernel (verifier is outside by design). Until
  answered, the objective is absent from any Pareto comparison.
- Lands in: `004` FR-013, kernel design sections 8 and 12, contract section 9 item 1.

### Q2. Typed attribution fields, or tags by convention?
- Spec: `001-consumer-usage-surface` FR-002 (U2, G2).
- Context: consumer, agent and session are not fields on the event or store; they would ride in free-form
  `tags`. U2 says typed, never inferred from tags.
- Options:

  | Option | Effect |
  |---|---|
  | A. Typed `consumer`, `agent`, `session`, `role` on event, store and query; `tags` kept for host extras | enforceable completeness test; schema change (minor if additive) |
  | B. Reserved tag keys (`moeka.consumer` and so on) validated by the kernel | no schema change; weaker typing, store still needs columns |
  | C. Typed `consumer` and `agent` only; session stays a tag | smaller change; session queries need tag parsing |

- Recommended: A. Reason: U9 requires an unattributed token to fail a test, which is only crisp with typed
  required fields; the store needs columns to query by them anyway (the SQLite table has no tags column).
- Lands in: `001` FR-002, plan design decisions, T007; kernel design U2 and G2.

### Q3. Where does the waste label live?
- Spec: `001-consumer-usage-surface` FR-010 (U10, G8).
- Context: retries, failovers and post-admission refusals are known when the call event is emitted; a
  discarded draft (rejected by a verifier or superseded) is known only afterwards. Events are append-only.
- Options:

  | Option | Effect |
  |---|---|
  | A. A `waste` field on `model.call` only | simple; cannot label a draft rejected later |
  | B. A separate `call.waste` event linked by `call_id`, for every case | one mechanism; every total needs a join |
  | C. Field on `model.call` for kernel-known cases, linked event for caller-known cases | immediate labels where possible, no mutation of past events; two mechanisms |

- Recommended: C. Reason: matches U10's "label set by the caller or the kernel", keeps the stream append-only,
  and the query layer hides the join from consumers.
- Lands in: `001` FR-010, T021; kernel design U10 and G8.

### Q4. What replaces `BudgetHalt` as the stop contract?
- Spec: `002-kernel-api-for-consumers` FR-008 (G12). Related: `001` FR-004, FR-015.
- Context: awork's `BudgetHalt` is a `BaseException` so its best-effort `except Exception` sites cannot swallow a
  spend stop; moeka's `BudgetExceeded` is an ordinary `LLMError`. The mapping lives only in awork's shim, and
  agent runs and batches report refusal differently (stop reason, systemic error).
- Options:

  | Option | Effect |
  |---|---|
  | A. Document one stop-contract table and leave `BudgetHalt` in awork | no kernel change; each consumer maps; the table is tested |
  | B. Kernel ships a `BaseException`-derived stop type or helper that consumers re-raise | uniform across consumers; a kernel class that bypasses `except Exception` is surprising and risky for embedders |
  | C. Kernel offers an opt-in hook `on_budget_stop` the host raises from | host chooses semantics; more surface |

- Recommended: A. Reason: a kernel raising `BaseException` would be an unwelcome default for other embedders
  (constitution VI favours the smaller surface); the contract table plus a test gives the single documented stop
  semantics the gap asks for.
- Lands in: `002` FR-008, T004; `docs/migration-moeka-api.md` Behaviour differences.

### Q8. How does the gateway move onto `moeka.Kernel`, and in what order?
- Spec: `003-main-consolidation` FR-009.
- Context: the gateway uses the legacy loop path and passes no plugin registry; consolidation is chosen, not
  executed. Kernel design section 12 lists "how the gateway moves onto `moeka.Kernel`" and "the order of
  steps" as open sub-questions.
- Options:

  | Option | Effect |
  |---|---|
  | A. Consolidate with the gateway unchanged, then migrate it to the kernel API behind a switch, then delete the legacy path | smallest first risk; two stages; live bot unaffected by stage 1 |
  | B. Migrate the gateway on `core-slim`-side first, then consolidate | gateway code does not exist on `core-slim`; not feasible without restoring it |
  | C. Do both in one step | one review; large blast radius on the live bot |

- Recommended: A. Reason: stage 1 is mechanical and testable; stage 2 can ship per channel behind a switch.
- Status 2026-10-01: stage 1 executed as A on branch `consolidate/new-main` (gateway on the legacy loop path via
  the deprecated facade seams); the owner still confirms A for stage 2. Open items: `003` plan, "Stage 2 remaining".
- Lands in: `003` FR-009, T003 and T014.

## Questions with recommended defaults (no marker)

### Q5. `Paths` derivation
- Source: kernel design section 12. Default: two host attributes; `sessions_root`, `data_dir`, `logs_dir` under
  `state_dir`; `media_dir` under `work_dir` because agents must reach attachments.
- Options: accept the default; put `media_dir` under `state_dir` (agents lose access to attachments).
- Recommended: accept the default (owner may veto). Already implemented per design I2.

### Q6. AST guard scope and `open_vec_store`
- Source: kernel design section 12; also `003` plan (gateway packages read process state).
- Options: A. keep the guard over the kernel packages and add a reviewed host-side allow-list for gateway
  packages; B. guard `nanobot/` as a whole and make each gateway read a named exemption.
- Recommended: A, and confirm `open_vec_store` stays a public factory that requires an explicit path (its
  signature already does). Lands in `003` FR-004, T013.

### Q7. Who builds the deterministic-solver registry
- Source: kernel design section 12. Default: plugins register solvers per task type; the kernel owns only the
  lookup. Options: kernel ships generic solvers (regex, AST) itself. Recommended: the default (constitution VI,
  generic mechanics only).

### Q9. Cross-process stream transport (U6, G5)
- Spec: `001` FR-006.
- Options: A. tail of a host-owned JSONL file; B. local socket; C. OTLP export; D. in-process only (rejected by
  U6 for the gateway).
- Recommended: A first (it doubles as the protected source in Q10; no new dependency), with the transport behind
  a small interface so B or C can follow.

### Q10. Forge-resistant usage source (U5, G10)
- Spec: `001` FR-005, SC-007.
- Options: A. host `TraceSink` stream as the source of truth, SQLite as a projection; B. a separate append-only
  file outside agent reach, protected by the file floor in both layouts; C. require a sandboxed exec backend.
- Recommended: A plus B (the floor must name `llm_usage.sqlite3` in the flat layout too), and say plainly that
  only C closes the exec case (design I3 and section 9 limits).

### Q11. How a response-cache hit is reported (U3, G3)
- Spec: `001` FR-003.
- Context (verified): a hit emits `cache.hit` (`call_id, key, alias, model, tags`), not `model.call`, and has no
  saved-token figure; the `cached` field on `model.call` is therefore not set by LLM-layer hits.
- Options: A. emit `model.call` with `cached=True` (breaks "one event per physical attempt"); B. extend
  `cache.hit` with attribution, saved tokens and saved cost, and union it in the usage API.
- Recommended: B.

### Q12. Consolidation direction
- Spec: `003` plan step 4.
- Options: A. branch from `main`, bring kernel paths from `core-slim`, hand-merge 201 shared files; B. branch from
  `core-slim`, restore the deleted gateway directories from `main`, restore removed hooks.
- Recommended: decide by a dry run counting manual hunks (T005); whichever direction is chosen, every hook that
  `core-slim` removed from the 201 shared files must be restored. Both preserve the owner's rule
  (gateway from `main`, kernel from `core-slim`).

### Q13. `DocStore` enumerate/export for corpus migration
- Spec: `002` FR-006 (derived, not an owner ruling). Context: the migration page says `DocStore` has no
  "list every chunk" call, so moving a collection needs raw SQLite reads.
- Options: add enumerate/export; or keep the documented one-off script path. Recommended: add it (consumers
  must not read store tables; constitution IX in spirit), or drop the FR if awork prefers re-ingesting.

## Inconsistencies found while writing the specs (verified 2026-09-30; none edited)

- Content-free versus replay: U3 says events carry no prompt or response text; U11 needs recorded responses.
  Resolved in `001` by making recordings a separate, host-owned, opt-in artifact (FR-003, FR-011). Owner to confirm.
- Cache hits: the migration page and design G3 treat `cached` on `model.call` as the hit marker, but the code
  emits `cache.hit` for hits (Q11); the design left it "needs checking".
- `usage_sink` replacement: the migration page says subscribing to `model.call` replaces the legacy payload,
  which has `cache_write_tokens`; the event has `tokens_cache_read` only (G3), so the figures are not equivalent
  until `001` T010.
- Identifier clash: kernel design section 14 uses K1-K11 for assumptions; the RSI design uses K1-K6 for kernel
  prerequisites. `004` spells out "harness K*n*".
- System contract section 2 states awork-resume's rule as "maker != checker (different model family, separate
  context)"; section 3a (which wins) says the same model is acceptable and only the same agent is an
  anti-pattern. The constitution follows 3a.
- `CLAUDE.md` in this worktree presents `MoekaKernel`/`MoekaCore` and the `Nanobot` SDK as the embedding
  surface and names a harness driven by `MoekaKernel`; the design and the RSI design say `moeka.Kernel` is the
  public API, the facade is deprecated and the harness never uses it. Update with `002` T016.
- The design cites a public-API plan ("Tasks 1-13" built, "Tasks 14-15" pending); no such plan file exists in
  this worktree, so Task 15 (shim removal) has no local source. `002` restates it as tasks T014-T017.
- Ambient-read guard: the design says the AST test covers `nanobot/` outside compat and CLI modules; on `main`
  the gateway packages contain ambient-read patterns, so the guard cannot cover a consolidated tree without a
  reviewed host-side allow-list (`003`, Q6).
- Contract section 8 R1 says the kernel design predates the last kernel commits; the design's header says it was
  re-checked on 2026-09-30. Treat R1 as resolved for the kernel design.
- RSI design numbers its sections 14, 16, 17, 15 out of order (cosmetic; branch not edited).
