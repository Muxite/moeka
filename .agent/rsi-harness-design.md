# RSI harness for moeka-core: design

Status: draft for review, 2026-09-25; revised 2026-09-28 against the moeka kernel
API (core-slim `5b9c7d43`, `docs/python-sdk.md` there); prerequisites and pin
re-checked 2026-09-30 (section 4.4, section 16). Nothing in this spec is
built: no harness repo, no task suite (M3), no M1 Docker result. Research basis: `.agent/rsi-daemon-feasibility.md` and
`.agent/rsi-and-agent-core-report.md` (both predate the kernel API; where they
name `MoekaCore`, `AgentHook` or `ToolLoader tools_allow`, section 4 here wins).
Implementation plan: `.agent/rsi-harness-impl-plan.md`.

## 1. Intent

Build a containerized, start/stop-anytime daemon that runs recursive
self-improvement (RSI) on **moeka-core**, the general agent core and its tools,
while the owner is away from the computer. The owner has no fixed target task.
The purpose is to **observe what an RSI loop does and what kinds of improvement
it finds**. So the daemon needs a fitness signal that is broad and honest, and
an observability layer that makes its behaviour legible.

Stated constraints (from the owner):
- Runs on demand ("turn on whenever"), on a single RTX 5070 Ti (16 GB), a local
  model only. The only frontier input is Claude Code authoring the initial tool
  set and tasks now and then.
- While running it reserves and saturates the GPU. If it has spare capacity it
  may expand to use it.
- Must be stoppable with about 2 minutes of notice and lose little.
  Checkpointing is frequent. Git branches and pushes are the save mechanism.
- Containerized. Harness repo with moeka as a submodule. The remote does not
  exist yet, so v1 is local-only.
- moeka's communication layer (channels, gateway, WebUI) is out of scope for
  RSI. A separate "cut-down moeka" development push happens first.
- vLLM is approved as the serving stack.

## 2. Non-goals
- Improving the model weights. This is scaffold-level improvement only.
- A public benchmark score. Public benchmarks are a regression tripwire only.
- Auto-shipping anything to another system. Winners land on branches for review.
- Server-management specialisation. moeka is treated as a general tool-using core.

## 3. Repository layout

```
moeka-rsi/                     harness repo (the judge; not mutable by the daemon)
  core/                        git submodule: moeka core-slim, pinned (the kernel)
  runner/                      rollout entrypoint: variant tree -> Kernel + AgentSpec
  supervisor/                  claim-run-record loop, gates, stop protocol
  evalsuite/                   tasks, fixtures, verifiers, reference solutions
  archive/                     schema and migrations (data lives on a volume)
  mutator/                     prompts and policy for proposing changes
  papers/                      paper-finding module
  compose.yml                  supervisor + vllm + rollout runner
  docs/                        journal templates, runbooks
volume (not in the repo):
  variants.git                 candidate variant trees, one branch per candidate
  archive.db, traces/, tests-traces/, bare remote
```

- The mutator can write **only inside a candidate's variant tree** (section 7),
  on that candidate's scratch branch of `variants.git`.
- `core/` is pinned and read-only to the daemon in v1: every v1 mutation target
  is something the kernel loads per `Variant` or `AgentSpec`, so no candidate
  needs a `core/` diff. `core/` becomes writable only with tier 2 (code
  mutation).
- The evaluator, gate, task suite, runner, supervisor and promotion logic are
  outside the mutator's write scope. This structural boundary is the main
  defence against the evaluator tampering seen in DGM and STOP.

## 4. The subject: moeka kernel (core-slim)

### 4.1 Status
- `core-slim` exists: channels, gateway, WebUI, bridge and pairing are gone;
  the agent loop, runner, providers, tools, skills and memory remain, fronted by
  the `moeka` package (a re-export of `nanobot.kernel`). `MoekaCore` is
  deprecated legacy; the harness never uses it.
- The 5 parked kernel residuals are fixed (`c8a9cd08..5b9c7d43`): rewind after
  `/new` keeps the pre-`/new` archive, `AgentSpec.memory_key`, the dedicated
  `Kernel(action_workers=8)` pool, shared usage store survives one kernel
  closing, identity prompt names the real memory files.
- This diverges from the upstream sync policy in `CLAUDE.md`, as expected.

### 4.2 How the harness drives the kernel
- **One kernel per rollout.** `Kernel(Environment.for_host(...), variant=...,
  budget=..., solvers=SolverRegistry(), baselines=BaselineRegistry())`, used as
  a context manager and closed when the rollout ends. Per-kernel `variant` and
  registries mean nothing leaks between rollouts even in one process.
- **Environment** (`Environment.for_host`, no ambient reads):
  - `state_dir` a fresh temp dir inside the rollout container; `work_dir` the
    seeded fixture copy (`strict=True` keeps them disjoint and denies file
    tools `state_dir`).
  - `providers=[ProviderSpec(name="vllm", api_base=<sidecar>)]`,
    `models=[ModelSpec(name="local", model=<served id>, provider="vllm",
    tier="local", context_window=...)]`, `default_model="local"`.
  - `trace=JsonlTraceSink(<out>/trace.jsonl)`, `exec_base_env={PATH, HOME}`,
    `offline=True`.
- **Agent.** `kernel.agent(AgentSpec(name="rollout", offline=True,
  tools_allow=<task family's tool set>, sampling=Sampling(seed=<seed>, ...),
  limits=RunLimits(max_iterations=..., deadline_s=...), memory=False))`.
  - `tools_allow` is always explicit and comes from the task, not the variant.
  - `system_prompt` and `bootstrap` stay unset on the spec: they would win over
    the variant's `bootstrap`, so persona text has one owner (the variant).
  - `offline` removes web tools, MCP and declared network capabilities; `exec`
    can still open sockets, so the container network (only the vLLM sidecar
    reachable) is the real boundary.
- **Rollout identity** is span tags, not a kernel id:
  `with kernel.trace.span("rollout", rollout=<id>, task=..., seed=...,
  candidate=..., tier=..., pool="practice"|"test"):` around
  `agent.run_sync(prompt, session=<task key>)`. Every event is stamped with the
  tags, the root span's `trace_id`, and span path `rollout/agent.run`.
- **Config hash** is `agent.fingerprint().digest` (system prompt, tool
  definitions, model, sampling; paths and memory normalised out), taken before
  the run and stored with the result. Until prerequisite K1 lands it misses
  non-always-on skill bodies, so the result cache key is (task, seed,
  fingerprint digest, variant tree hash, `core/` commit).
- **Budgets.** `CapBudget(limit_tokens=N)` per kernel. Local-tier models cost
  `0.0`, so USD caps, `per_tag` caps (USD-only) and `baselines.cost_ratio` are
  degenerate here; token caps and `RunLimits` are the enforced limits. The
  container's CPU, memory and wall-clock limits sit outside all of them.
- **Outputs** per rollout: `trace.jsonl`, `result.json` (the `RunResult`:
  `stop_reason`, `iterations`, `usage`, `tools_used`, `messages`, `trace_id`,
  error as text), `fingerprint.json`, and the final `work_dir` for the verifier.
- **Fixtures** never ship a `skills/` directory: `<work_dir>/skills` would
  shadow the variant's skills. The verifier fails a rollout that creates one.

### 4.4 Pin note (2026-09-30)
- The spec was written against `5b9c7d43`. Since then `core-slim` gained only a
  merge of `main`'s upstream sync (`d5f4e7d0`, `41e10944`, `6f80c392`: session
  inbox, compaction, memory sanitization; `nanobot/agent/runner.py` and loop
  tests changed, some `test_runner.py` cases dropped in `b1530a0c`) and docs
  commits. Nothing under `nanobot/kernel/` or `moeka/` changed, so the kernel
  API this spec relies on is the same at `6f80c392`. The test suite is not the
  same, though, so a green run on `5b9c7d43` would not cover the tip.
- Both `5b9c7d43` and `6f80c392` are on `origin/core-slim`. awork's unmerged
  compat branch pins `6f80c392`; pinning the harness to the same commit keeps
  one kernel version across consumers. Not yet decided (M1 records the pin).
- Owner direction 2026-09-30: work may consolidate onto each repo's `main`
  (see the kernel design, "Decisions for the owner"). M1 pins `core-slim`, and
  that consolidation may move the pin to `main`. Not decided; do not rebase
  `core-slim` meanwhile.

### 4.3 Kernel prerequisites
Gaps the harness needs closed in core-slim, each tagged with the first
milestone that needs it. All have interim workarounds; none blocks M2.
- **K1 skill content in the fingerprint** (M4). Today the fingerprint sees the
  skills summary and always-on skill bodies only.
  API: `Fingerprint.components["skills"]` = sha256 over sorted (relpath, bytes)
  of the effective skills tree (`builtin_skills_dir` after include/exclude, plus
  `inline_skills`).
- **K2 tool parameter-description overrides** (M5). `Variant` overrides only
  the top-level tool description; parameter schemas are not mutable.
  API: `Variant.tool_param_descriptions: Mapping[str, Mapping[str, str]]` (tool
  -> {parameter path -> description}); structural schema keys rejected.
- **K3 argument digest on `tool.call`** (M4). Needed for loop/repeat rate and
  "same call retried" without parsing transcripts.
  API: `tool.call.args_digest` = sha256 of the canonical arguments JSON.
- **K4 `RunLimits.max_tool_errors`** (M5). Declared but raises
  `NotImplementedError` in `kernel.agent`.
  API: implement it; the run ends with `stop_reason="tool_error"` at the cap.
- **K5 strict sampling on agent runs** (M3). `on_unsupported="raise"` exists
  only on `GenerateOptions`; an agent run with a dropped `seed` just emits
  `sampling.dropped`. API: `AgentSpec(on_unsupported="raise")`, ending the run
  with `stop_reason="error"` and `UnsupportedRequestError`. Interim: the runner
  marks any rollout with a `sampling.dropped` event non-reproducible.
- **K6 skill reads outside `read_file`** (M5, optional). `skill.read` fires only
  when `read_file` opens a `SKILL.md`; `exec cat` or `grep` is invisible.
  API: emit `skill.read` with `via=<tool>` from any `fs.read` capability check
  whose resource is a `SKILL.md`. Interim: `skill.read` is a lower bound.

Status of K1-K6, checked against core-slim `6f80c392` on 2026-09-30: all six
are open (none is implemented).
- K1 open: `variants.fingerprint` components are `system_prompt`, `tools`,
  `model`, `sampling` only (`nanobot/kernel/variants.py`); no `skills` component.
- K2 open: `Variant` has `tool_descriptions(_dir)` but no
  `tool_param_descriptions`.
- K3 open: the `tool.call` event (`nanobot/kernel/trace_hook.py`) carries no
  `args_digest`.
- K4 open: `RunLimits.max_tool_errors` still raises `NotImplementedError`
  (`nanobot/kernel/agent.py`).
- K5 open: `AgentSpec` has no `on_unsupported`; it exists only on
  `GenerateOptions` (`nanobot/kernel/llm.py`).
- K6 open: `skill.read` is emitted only from `read_file`
  (`nanobot/agent/tools/filesystem.py`).

Deliberately not prerequisites:
- Rollout id: span tags plus `trace_id` cover it.
- Transcript: `RunResult.messages` carries tool arguments and results.
- Outcome score: the verifier runs outside the rollout container, so the kernel
  cannot see it. The supervisor writes a harness-owned `rollout.scored` record
  keyed by the rollout tag. There is no host hook seam on kernel agents
  (`AgentSpec` takes no hooks), and none is wanted.

## 5. Components

1. **Supervisor** (plain Python, not an LLM). Durable task table in SQLite;
   claim, run, record. Killable at any instant; on start, tasks left `running`
   become `pending`.
2. **Run gate.** Runs work only when the GPU is available: `nvidia-smi`
   utilisation, the `gpu-lock` state (acquire with a TTL, renew, poll its chat
   for "please free" messages), and a user-activity flag. Adds hysteresis so it
   does not thrash vLLM startup.
3. **Serving sidecar.** vLLM with `openai/gpt-oss-20b` (fallback
   Qwen3-14B-AWQ), prefix caching, `--gpu-memory-utilization` about 0.88,
   `--max-num-seqs` starting at 16 and raised when headroom exists. Exposed to
   moeka through `api_base`. The image must be smoke-tested on this Blackwell
   card before anything depends on it.
4. **Rollout runner.** Builds one kernel per rollout from a variant tree
   (section 4.2) in a fresh container whose only reachable endpoint is the vLLM
   sidecar, on a seeded fixture copy. Rollouts are short and idempotent, with an
   id of (task, seed, candidate) carried as span tags; the config hash is the
   agent's fingerprint.
5. **Verifier.** Deterministic; runs outside the rollout container on a copy of
   the final state, read-only to the agent. No LLM judge in any gate.
6. **Mutator.** Proposes one change per candidate on a scratch branch of
   `variants.git`, using the local model.
7. **Archive.** SQLite: variant id, parent id, git commit, score vector,
   transcripts, gate verdicts, source (self-proposed or paper id). Open-ended.
   The **active** set (what moeka actually loads) is capped and separate.
8. **Paper module.** See section 10.
9. **Journal.** See section 9.

## 6. The loop

```
gate open? -> pick task type:
  eval-baseline   score current best (fills the archive, measures noise)
  mutate          pick parent (score + novelty + descendant success)
                  -> one change to a variant tree on a scratch branch
                  -> tier 0 smoke -> tier 1 dev subset -> tier 2 full dev
                  -> pass gate? admit to archive : discard (kept as evidence)
  promote         occasional locked-set canary; tag the variant as best
  paper-cycle     see section 10
every step writes SQLite first; a killed run resumes from the last row
```

Gate rules (deterministic, pre-commit, from the literature review):
- Candidate must not regress any regression task, and must beat its parent by a
  paired margin on the dev tiers. Three outcomes: pass, fail, unresolved
  (unresolved gets more samples, capped).
- Held-out conformance replay, not the session's own verifier. Rejected
  candidates never enter the context used to write later changes.
- Each new skill is checked for description collisions with existing ones and
  must declare preconditions and environment assumptions.
- Admission stores a certificate: content hash, gate verdicts, scores.
- Active skill set has a hard cap. Retirement is driven by measured
  per-skill contribution recorded in an append-only evidence log.

## 7. Mutation scope

**Decided (owner, 2026-09-25): v1 mutates tools, skills and prompts only.**
"Tools" means the definitions the model sees; no new tool implementations
(decided, section 15). A candidate is a variant tree; each target maps to a
kernel field:

```
variants/<candidate>/
  variant.toml          name, parent, hypothesis, AgentSpec knobs below
  skills/<name>/...     -> Variant.builtin_skills_dir (a full tree, seeded from core/)
  templates/agent/...   -> Variant.templates_dir (per-file shadowing)
  tool_descriptions/    -> Variant.tool_descriptions_dir (<tool>.txt)
  bootstrap/*.md        -> Variant.bootstrap (AGENTS.md, SOUL.md, USER.md, extras)
```

- **Skills** (`SKILL.md` packages): `Variant.builtin_skills_dir`. The active set
  and its cap: `AgentSpec.skills_include` / `skills_exclude` (from
  `variant.toml`), also the lever for per-skill ablation.
- **System prompts**: `Variant.templates_dir` for the identity, tool contract
  and skills-section templates (main and sub-agent prompts; memory and runner
  templates stay built-in); `Variant.bootstrap` for persona sections.
- **Tool descriptions**: `Variant.tool_descriptions_dir` (or the in-memory
  `tool_descriptions`). Applies to loader-built tools only; host actions and MCP
  tools keep their own text.
- **Tool parameter schemas**: not mutable until K2; then parameter
  descriptions only, never structure (the implementation must still accept it).
- **Sampling**: `AgentSpec.sampling` (`temperature`, `top_p`, `top_k`, `min_p`,
  penalties, `max_tokens`, `reasoning_effort`, `stop`). `seed` is rollout
  identity, set by the runner, never by the mutator.
- **Run knobs**: `RunLimits.max_iterations` within a harness ceiling.
  `deadline_s`, the token budget and `tools_allow` are harness-owned.
- **Fixed in v1**: the model alias and `ModelSpec` (one served model), `memory`
  (off), `offline` (on).

Deferred: core and tool code mutation (former "tier 2"). Revisit only after
the v1 loop has a track record.

Never mutable: the evaluator, gate, runner, supervisor, task suite, verifiers,
secrets, git credentials, `core/`, and anything outside the candidate's variant
tree.

## 8. Evaluation

No fixed objective, so fitness is **general tool-use competence** on a
Claude-Code-authored suite, plus cheap proxies.

- **Suite:** starts at about 60-90 tasks and grows toward about 300 across
  five families: filesystem and text, shell and processes, structured data and
  mock APIs, error recovery, and tool selection with distractors. Deterministic
  state-diff verifiers only.
- **Held-out tests (owner requirement):** the agent and the mutator never see
  the tests. No task text, fixtures, verifier code, task ids, transcripts or
  failure traces from any test cross the boundary. What they get back is a
  coarse verdict: per candidate, pass/fail/unresolved plus an aggregate score
  delta, and family-level deltas rounded coarsely (each family has enough
  tasks that one task's outcome cannot be read off, and a minimum family size
  is enforced). Query volume against the tests is rate-limited and counted.
  Tests are split 75% gate set (tiers 1-2) and 25% locked set (tier 3); both
  are hidden, and the locked set is used sparingly. Claude Code rotates in
  fresh tests periodically.
- **Practice pool (what the agent can learn from):** a separate set of tasks,
  disjoint in content from the tests, that the agent can run, read traces
  for, and learn from. It has two sources: (a) Claude Code-authored practice
  tasks in the same families as the tests but different content, and (b)
  tasks the agent proposes for itself in a sandbox, with self-checks that
  are weak by design (never used in a gate). Improvements must show up on the
  hidden tests to count, so practice-set overfitting is caught by the gate.
- **Task admission audit:** the reference solution must pass; defect-injected
  variants and no-op, `touch`-everything and `rm -rf` agents must score 0;
  keep tasks with a local pass rate between 10% and 90%.
- **Noise first:** before any mutation, measure the unchanged agent's
  disagreement across two seeds. That number sets the required task count.
  Single edits may be worth +1-3 points, so the gate accumulates evidence over
  several candidates and the early levers are tool descriptions and tool-call
  validity.
- **Cascade:** tier 0 smoke (about 15 tasks), tier 1 dev subset (about 60,
  2 seeds), tier 2 full dev plus regression (3 seeds), tier 3 locked test
  (rate-limited), tier 4 public sanity (BFCL multi-turn slice plus about 20
  Terminal-Bench-lite tasks, no regression beyond about 2 points).
  Results are cached by (task, seed, config hash); the config hash is the
  fingerprint plus the variant tree hash and `core/` commit until K1 (4.2).
- **Proxies** (tie-breakers and continuous fitness, never overriding pass
  rate): tool-call validity, iterations and tokens per success, wall time,
  recovery-after-error, skill-selection accuracy, loop/repeat rate.
- **Verifier audit each epoch:** inject known-bad candidates and confirm the
  gate and retirement catch them. The judge's false-pass rate is a first-class
  metric ("The Blind Curator").
- **Leakage guard:** screen accepted skill text for task-specific strings and
  cap skill length. The locked set is the real check.

## 9. Tracing and logging (agent-readable) and observability

**Agent-readable traces (owner requirement).** The mutator reads its own
history to decide what to change, so tracing is designed for a model to read:
- Every practice rollout writes the kernel's event stream to its own
  `trace.jsonl` (`JsonlTraceSink`) plus `result.json` (the `RunResult`,
  including `messages`), both tagged by the rollout span (section 4.2).
- A derived per-rollout summary (plain text) and a per-skill and per-tool
  rollup (calls, invalid-argument rate, error rate, contribution to success)
  so the mutator does not need to read raw transcripts.
- Mutation history: every past candidate with its diff, hypothesis, gate
  verdict and outcome, queryable by the mutator (what was tried, what
  failed, what was retired).
- Traces are mounted **read-only** into the mutator's context. They contain
  practice runs only. A redaction step guarantees no held-out material enters
  them: test rollouts run in a separate container and write their traces to a
  store the mutator cannot mount. Only the coarse verdict crosses over.
- Human-readable logs share the same event stream (span tags carry rollout,
  candidate and phase), so the owner and the agent read one source.

**Metric sources.** Every metric the spec uses, and the event and field that
provides it (events per `EVENTS` in `nanobot/kernel/trace.py`; every event also
carries `trace_id`, `span`, `tags`, `ts`). "Harness" means computed by the
supervisor or verifier; "K*n*" is a kernel prerequisite (section 4.3).
- Rollout, candidate, task, seed, pool, phase: `tags` on every event.
- Config hash: `agent.fingerprint().digest` and `.components` (not an event);
  skill bodies need K1.
- Messages per turn, tool arguments and results: `RunResult.messages`, joined
  to events by the tool message's `tool_call_id` = `tool.call.call_id`.
- Stop reason: `run.completed.stop_reason` (includes `deadline`, `budget`,
  `max_iterations`, `policy_denials`).
- Iterations: `run.completed.iterations`; per step `iteration.tool_calls` and
  `iteration.finish_reason`.
- Tokens: `run.completed.usage` (run totals), `iteration.usage` (per step),
  `model.call.tokens_in` / `tokens_out` / `tokens_cache_read`.
- Wall time: `run.completed.ts - run.started.ts`; `model.call.latency_ms`;
  `tool.call.duration_ms`.
- Tool-call validity rate: share of `tool.call` with `args_valid=True`; detail
  in `tool.invalid.error`.
- Tool error rate: `tool.call.ok=False` by `error_kind` (`invalid_args`,
  `tool_error`, `result_invalid`, or an exception class); `tool.result_invalid`.
- Provider retries: `model.call.attempt > 1`.
- Tool retries and loop/repeat rate: repeated (`tool`, arguments) within a run.
  K3 (`args_digest`); interim, derive from `RunResult.messages`.
- Recovery after error: harness, a `tool.call.ok=False` followed by a passing
  outcome in the same rollout.
- Skills offered versus read (the old "retrieved versus selected"):
  `skill.listed.skills` and `.active` versus `skill.read.skill`. There is no
  retrieval step; every listed skill is offered. `skill.read` sees `read_file`
  only (K6).
- Skill-selection accuracy: harness, `skill.read` against the task's declared
  expected skill.
- Per-skill contribution: harness, `skill.read` joined with outcomes, confirmed
  by ablation (`skills_exclude`).
- Skill hashes: K1; interim, the variant tree hash per skill directory.
- Policy denials: `policy.decision` with `verdict="deny"`.
- Budget used: `budget.admit` / `budget.refuse` (`worst_case_tokens`),
  `CapBudget.spent_tokens`. `cost_usd` is `0.0` on the local tier; do not use it.
- Seed honoured: absence of `sampling.dropped` (K5 makes it a hard failure).
- Outcome (pass/fail, score): harness-owned `rollout.scored` record written by
  the supervisor after the verifier; no kernel source, by design.
- Gate evidence, verdicts, judge canary results: harness.

**Journal.** Because the goal is to see what RSI does, the journal is a
first-class output:
- Per candidate: what changed (diff), the mutator's stated hypothesis, score
  deltas by family, gate verdict, and whether it was later retired.
- A running changelog of admitted improvements, grouped by type (skill, prompt,
  tool description, sampling or run knob, and code once tier 2 exists).
- Periodic summary reports: acceptance rate, which mutation types succeed,
  score trajectory against the locked set, verifier audit results, and
  suspected gaming (for example large proxy gains with no pass-rate gain).
- Every run logs the fingerprint, skill hashes, skills offered versus read,
  gate evidence, budget used, and judge canary results (sources above).

## 10. Paper-finding module

Daily poll of arXiv (cs.AI, cs.CL, cs.LG), HF daily papers and OpenAlex, with
Semantic Scholar for citation expansion (heavily rate-limited). Two-stage
filter: embedding similarity to a standing profile, then a local yes/no with
a structured reason. Distil each keeper into a hypothesis (change, expected
effect, how to measure), reject those needing frontier models, training or
missing resources, then run the change through the section 6 gate. Record the
paper id with the outcome so failed ideas are not retried, and track how often
a paper's claimed gain replicates. Paper text is untrusted data; the
implementer step has no network.

## 11. Checkpointing and stop protocol

- Every step: SQLite (WAL) on a bind-mounted volume.
- Every few minutes and on every admission: commit to a WIP branch in
  `variants.git`, plus a text snapshot of the archive into the harness repo
  (binary SQLite diffs badly).
- On stop notice (SIGTERM): stop scheduling; give in-flight rollouts about 60
  seconds; flush partial transcripts as resumable; commit; if a remote exists,
  push with a hard timeout; release the GPU lock. If the push fails, the volume
  already holds everything and the next start retries it.
- The vLLM container is stateless, uses `stop_grace_period` about 90 s, and
  stays warm across idle periods.
- The remote does not exist yet, so v1 saves to a local bare repo on the
  volume. Adding a remote later means setting one URL and a repo-scoped deploy
  key, mounted read-only.

## 12. Isolation

- Candidate rollouts run in containers whose only network peer is the vLLM
  sidecar (an internal compose network), with resource limits and no access to
  the evaluator or the archive. Mock APIs are harness-authored host actions
  (`AgentSpec.actions`) or processes inside the rollout container.
- Do not rely on moeka's shell sandbox, which is permissive by default in this
  fork. The container is the boundary. Non-root user, dropped capabilities,
  read-only root filesystem, workspace volume only.
- The verifier verifies claims about tool use against harness logs, not the
  agent's own transcript.
- Every edit outside the candidate's variant tree is logged and fails the
  candidate.
- Per-run token, time and step budgets are enforced by the kernel
  (`CapBudget`, `RunLimits`, `deadline_s`) and backstopped outside it by the
  container's limits and the runner's kill timer.

## 13. Milestones

1. Cut-down moeka (`core-slim`) passing its tests. Essentially done (4.1).
2. Compose stack: vLLM smoke test on the 5070 Ti, then the supervisor running a
   no-op task through stop, kill and resume.
3. Task suite v0 (30 tasks) with the audit, plus the noise measurement.
4. Baseline scoring, archive, journal.
5. Tier 1 mutation loop with the gate.
6. Paper module.
7. Decide on tier 2 based on what tier 1 produced.

## 14. Risks
- Blackwell sm_120 support in vLLM is unverified on this machine.
- A 7-32B local model may gain little from self-authored skills (in one paper,
  LLM-authored skills gave +0.0 points against +16.2 for human-curated). The
  initial tool set is therefore authored by Claude Code, and the daemon leans
  on selecting, verifying and pruning.
- Small suites cannot resolve small gains, so early results are directional.
- Concurrent GPU users. `gpu-lock` is advisory, so the daemon must poll and
  yield rather than assume it is enforced.
- Tier 2 (code mutation) is a larger blast radius and needs the stricter gate.

## 16. Assumptions and risks
What this design assumes without proof. PROVEN needs a test or measurement;
nothing below the kernel mechanics has either yet.
- **Objectives measure usefulness (UNPROVEN).** The harness optimises the
  kernel design section 8 objectives: held-out quality, cost per task,
  provenance rate, clarification yield, denial rate, latency. Each is a proxy.
  Cost per task as a proxy for value is kernel invariant I6, itself unproven.
  Provenance rate counts resolving cites, not whether the fact supports the
  value. Clarification yield has no trace event at all (owner decision open in
  the kernel design), so it cannot be scored today.
- **A quality task suite can be built (UNPROVEN).** The suite is M3 and does
  not exist. Until it does, quality on held-out tasks is unmeasured and the
  acceptance rule (not Pareto-dominated, strictly better on one objective) has
  nothing to compare on its main axis. The spec's own task audit (section 8) is
  the mitigation, not a proof.
- **Mutations transfer (UNPROVEN).** The assumption is that a prompt, skill or
  tool-description change that wins on the practice pool helps on unseen
  tasks and on real use, and that a 7-32B local model benefits from
  self-authored skills. The only cited evidence points the other way (+0.0 for
  LLM-authored skills, section 14).
- **Evaluation is noisy (known, unquantified).** Local sampling, small suites
  and few seeds mean small gains are indistinguishable from chance; the noise
  measurement is part of M3, so the paired-margin gate is uncalibrated today.
  Expect directional results first.
- **The hard constraints are checkable (PROVEN as kernel mechanisms).** I1-I5
  have tests in the kernel (kernel design section 14, K1), but the harness that
  would run them as a candidate gate does not exist. They do not hold against an
  exec-capable agent without a sandbox.
- **Risks carried from elsewhere.** The pin may move (section 4.4). The kernel
  cost ledger can be forged under `exec`, so score cost from the trace stream.
  Shared GPU contention (section 14). No consumer other than this harness
  validates the kernel objectives; awork uses the kernel through legacy
  entry points and awork-resume does not use it at all.

## 15. Open items for the owner
Resolved by the owner: mutation scope is tools, skills and prompts only;
local-only saves are fine for v1; no-network rollouts with mock servers are
acceptable; tests are fully held out from the agent.

Decided: "tools" means the definitions the model sees (descriptions, and
parameter descriptions once K2 lands). No new Python tool implementations in
v1: no host actions, plugins or MCP servers authored by the daemon, and `core/`
stays pinned.

Still open:
- How much of the practice pool should Claude Code author up front versus
  letting the agent propose its own? Assumed: Claude Code seeds it, the agent
  adds to it.
