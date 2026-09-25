# RSI harness for moeka-core: design

Status: draft for review, 2026-09-25. Nothing in this spec is built. Research
basis: `.agent/rsi-daemon-feasibility.md` and `.agent/rsi-and-agent-core-report.md`.

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
  core/                        git submodule: cut-down moeka (the subject)
  supervisor/                  claim-run-record loop, gates, stop protocol
  evalsuite/                   tasks, fixtures, verifiers, reference solutions
  archive/                     schema and migrations (data lives on a volume)
  mutator/                     prompts and policy for proposing changes
  papers/                      paper-finding module
  compose.yml                  supervisor + vllm + sandbox runner
  docs/                        journal templates, runbooks
```

The mutator can write **only inside `core/`** (and only on scratch branches).
The evaluator, gate, task suite, supervisor and promotion logic are outside its
write scope. This structural boundary is the main defence against the evaluator
tampering seen in DGM and STOP.

## 4. Prerequisite: cut-down moeka (separate track)

Before the loop runs, produce a slim moeka branch (`core-slim`): channels,
gateway, WebUI, bridge and pairing removed, leaving the agent loop, runner,
providers, tools, skills, memory and MoekaCore. This diverges from the upstream
sync policy in `CLAUDE.md`, which is expected. Acceptance: the existing core
and tool tests still pass in Docker (`scripts/test-docker.sh`). This is planned
as its own task and does not block writing the harness.

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
4. **Rollout runner.** Runs moeka-core on a task in a fresh no-network
   container from a seeded fixture. Rollouts are short and idempotent, with an
   id of (task, seed, variant).
5. **Verifier.** Deterministic; runs outside the rollout container on a copy of
   the final state, read-only to the agent. No LLM judge in any gate.
6. **Mutator.** Proposes one change per candidate on a scratch branch of
   `core/`, using the local model.
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
                  -> one change in core/ on a scratch branch
                  -> tier 0 smoke -> tier 1 dev subset -> tier 2 full dev
                  -> pass gate? admit to archive : discard (kept as evidence)
  promote         occasional locked-set canary; commit to core/ branch
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
In scope: skills (`SKILL.md` packages), system prompts, tool descriptions and
schemas, config presets and sampling parameters. Reading "tools" as the
definitions the model sees (name, description, parameter schema, usage notes).
Whether the daemon may also author brand-new tool *implementations* is left
open (see section 15); until answered, no Python code in `core/` is mutated.

Deferred: core and tool code mutation (former "tier 2"). Revisit only after
the v1 loop has a track record.

Never mutable: the evaluator, gate, supervisor, task suite, verifiers, secrets,
git credentials, and anything outside `core/`.

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
  Results are cached by (task, seed, config hash).
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
- Every practice rollout writes a structured JSONL trace: per turn, the
  messages, each tool call with arguments and result, validity errors, retries,
  which skills were retrieved versus selected, tokens, wall time, stop reason.
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
- Human-readable logs share the same event stream (structured events with
  run id, candidate id, phase), so the owner and the agent read one source.

**Journal.** Because the goal is to see what RSI does, the journal is a
first-class output:
- Per candidate: what changed (diff), the mutator's stated hypothesis, score
  deltas by family, gate verdict, and whether it was later retired.
- A running changelog of admitted improvements, grouped by type (skill, prompt,
  tool description, config, and code once tier 2 exists).
- Periodic summary reports: acceptance rate, which mutation types succeed,
  score trajectory against the locked set, verifier audit results, and
  suspected gaming (for example large proxy gains with no pass-rate gain).
- Every run logs skill hashes, retrieval candidates versus the selected skill,
  gate evidence, budget used, and judge canary results.

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
- Every few minutes and on every admission: commit to a WIP branch in `core/`,
  plus a text snapshot of the archive into the harness repo (binary SQLite
  diffs badly).
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

- Candidate rollouts run in containers with no network, resource limits, and no
  access to the evaluator or the archive.
- Do not rely on moeka's shell sandbox, which is permissive by default in this
  fork. The container is the boundary. Non-root user, dropped capabilities,
  read-only root filesystem, workspace volume only.
- The verifier verifies claims about tool use against harness logs, not the
  agent's own transcript.
- Every edit outside `core/` is logged and fails the candidate.
- Per-run token, time and step budgets are enforced outside the agent.

## 13. Milestones

1. Cut-down moeka (`core-slim`) passing its tests.
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

## 15. Open items for the owner
Resolved by the owner: mutation scope is tools, skills and prompts only;
local-only saves are fine for v1; no-network rollouts with mock servers are
acceptable; tests are fully held out from the agent.

Still open:
- Does "tools" include authoring new tool implementations (Python), or only the
  tool definitions the model sees? Assumed: definitions only.
- How much of the practice pool should Claude Code author up front versus
  letting the agent propose its own? Assumed: Claude Code seeds it, the agent
  adds to it.
