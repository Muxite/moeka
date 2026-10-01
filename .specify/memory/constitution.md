# moeka Kernel Constitution

The kernel is the embeddable, secure execution core of moeka: the agent loop and runner, the capability policy
gate, the LLM layer and cost ledger, the generic fact and artifact stores, and the public `moeka` package.
Its consumers are engineers and agents (awork, the live gateway, the RSI harness, third-party embedders), not
end users. This constitution states the principles every spec, plan and change must satisfy. It states no
volatile facts (what is built, test counts, commit hashes); those live in dated status docs, chiefly
`.agent/moeka-kernel-design.md`, which is the detailed design and stays authoritative for mechanics.

## Core Principles

### I. Zero Ambient Reads (NON-NEGOTIABLE)
The kernel MUST NOT read the process environment, the home directory, or any config or path it was not handed.
Every path, credential and runtime flag enters through the host-built environment. Ambient reads are allowed
only in an explicit allow-list of host-side modules (CLI, config layer, legacy adapter). Child processes see
only the environment the host supplies. Proof: an AST guard over the whole package and a fake-HOME runtime test
(design section 3, I1).

### II. Physical Path Separation
The agent's working area and the kernel's state area MUST NOT overlap. Agent file and shell operations stay in
the working area; sessions, traces, stores, policy and the usage ledger stay in the state area. A strict
environment refuses overlap and refuses shell execution without a declared sandbox. Proof: construction-time
rejection tests and file-floor tests (design I2). Stated limit: in-process checks never hard-isolate shell
commands; real containment is a sandbox or container, and a spec MUST NOT claim more than the layer it relies on
can give (design section 4).

### III. Unearned Knowledge Is Forbidden
A value is committed to an artifact only when it is backed by a resolving provenance record (document span, tool
output, or user turn). An uncited value stays provisional and never reaches the committed artifact. A typed
result that fails its schema is an error, never a fact. "Committed" means the cite resolved, not that the fact
supports the value; any claim of groundedness MUST say which of the two it means (design I3, section 7).

### IV. Strict Capability Attenuation
A child agent or plugin receives only the intersection of its parent's grants and its declared manifest.
Privileges never broaden downstream. Floors (checked before policy) cannot be removed by rule, flag or config.
Every gate decision, allow or deny, is emitted to the host's audit stream; a failing sink never turns a decision
off. The gate binds declared capabilities only, and this limit MUST be stated wherever a containment claim is
made (design I4, sections 5 and 9).

### V. Hard Failure Limits
A turn MUST terminate when it exceeds its step budget, its deadline, or its policy-denial ceiling. Every run
reports which limit ended it as a typed stop reason. A limit that is declared but not enforced MUST raise, not
silently pass (design I5).

### VI. Pareto Simplicity and Cost Ceiling
For a sub-task with a target quality, expected cost MUST NOT exceed that of the cheapest adequate alternative.
A task solvable deterministically MUST NOT invoke a model; a task a compact model can solve MUST NOT dispatch a
frontier model without a recorded verification failure. This is a target enforced by measurement and routing,
not a theorem; a spec MUST say which part is enforced and which is only measured (design I6, section 6).

### VII. Everything Is Data (every token attributable and observable)
Every token the kernel causes to be billed or computed MUST be attributable to a call, a purpose (slot or role
plus task type) and a consumer, and observable as a content-free event. An unattributed token is a test
failure, not an empty column. Wasted tokens (retries, failover attempts, avoidable cache misses, discarded
drafts, calls refused after admission) are first-class, labelled metrics, not after-the-fact guesses. Observation
is fail-open: it never blocks or fails a model call. Observation is itself tested, because the owner's ruling
(2026-09-30) permits better or paid models only when tracing is so well tested that no token goes to waste
(design section 3b, U1-U12).

### VIII. Verification Independence
Any verifier, judge or epistemic audit that accepts or rejects a value MUST run in a context separate from the
producer: its own messages, no access to the producer's memory, scratchpad or reasoning, and an evidence-only
view (the claim plus the cited sources). A different model is better than the same model; the same model in a
separate context is acceptable; the same agent (same context or memory) verifying its own output is an
anti-pattern and MUST NOT be offered as a default. Gates use deterministic checks and held-out tests before any
model judge (design section 7; owner ruling 2026-09-30).

### IX. Consumers Derive Usage From moeka
Usage, spend and budget figures shown by a consumer MUST be derivable from moeka's public API. A consumer
MUST NOT need its own ledger, nor import `nanobot.*` internals, nor read moeka's SQLite files directly. The
figures a consumer trusts MUST come from a source the agent under observation cannot rewrite (owner ruling
2026-09-30; design section 3b, U1 and U5). awork-resume is independent of moeka by owner decision and is not
bound by this principle.

### X. Cite Only Verified Sources
A design decision may cite a paper or software only if its identity (title, authors, id) was verified. The
status (`verified`, `abstract read`, `unverified lead`) and the decision it supports MUST be recorded, and no
invariant, number or decision may rest on a citation alone. Invented or unchecked citations are defects
(design section 11).

## Additional Constraints

- **Requirements versus facts.** A spec states requirements and end goals. Facts about current code (what
  exists, statuses, counts, commit hashes) appear only in `plan.md` and `tasks.md`, dated "as of YYYY-MM-DD",
  and MUST be verified against code when written.
- **Honest proof.** Every requirement names the test or measurement that proves it. A requirement with no
  proof is listed as unproven, never implied met. Proxies (cost per task, provenance rate, denial rate) are
  labelled as proxies.
- **Consumer compatibility.** Changes to a seam a consumer uses MUST keep I1-V. Legacy entry points are removed
  only after every consumer pins a migrated commit. A consumer pin is never invalidated by a rebase of a pinned
  branch.
- **Mutation tiers.** Self-improvement may edit skills, prompts, tool descriptions and tuning config behind a
  harness gate. Plugin code, core code and policy, sandbox, evaluator, credential resolver and plugin pins are
  progressively stricter; policy and the evaluator are never self-editable (design section 9).
- **Live service protection.** The live gateway runs from a working tree; work on other branches uses
  worktrees, never a checkout in the live directory.

## Development Workflow

- The spec-kit flow applies: constitution, then spec (what and why), clarify, plan, tasks, analyze. Specs
  prioritise independently testable consumer stories and testable `MUST` requirements; measurable success
  criteria are technology-agnostic; at most three `[NEEDS CLARIFICATION]` markers per spec, and only for open
  owner decisions.
- Every plan has a Constitution Check gate; a violation needs an entry in Complexity Tracking with the simpler
  alternative rejected.
- Tests come first for gap-closing work: a failing test that encodes the requirement precedes the change.
- Commits are small, with short lowercase subjects and no AI co-author trailers (owner preference, 2026-09-30
  memory rule).
- Docs diagrams use mermaid only; design prose uses firm bullets, tables only for tabular data.

## Governance

- This constitution supersedes other practice docs. Where it conflicts with a spec, plan or task, the
  constitution wins; where owner rulings (system contract section 3a) conflict with anything, the rulings win
  and this constitution is amended to match.
- Amendment: a proposed change names the principle, the reason, the migration for existing specs, and the
  owner's approval. The version bumps by semantic rule: MAJOR for removing or redefining a principle in an
  incompatible way, MINOR for adding a principle or materially expanding guidance, PATCH for wording. Each
  amendment updates "Last Amended" and lists affected specs.
- Compliance: every plan runs a Constitution Check before research and after design; every review verifies
  compliance; any unmet principle is recorded in Complexity Tracking or blocks the change.
- The detailed mechanics of I1-VI (status, proofs, limits) live in `.agent/moeka-kernel-design.md` and are not
  duplicated here. A change to an invariant's mechanics updates the design; a change to its meaning amends this
  file.
- Open owner questions live in `specs/CLARIFY-LOG.md` until answered, then are encoded into the owning spec.

**Version**: 1.0.0 | **Ratified**: 2026-09-30 | **Last Amended**: 2026-09-30
