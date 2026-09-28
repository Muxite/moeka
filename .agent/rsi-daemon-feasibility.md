# Feasibility: containerized, opportunistic, checkpointed RSI daemon on moeka

*Predates the moeka kernel API (`MoekaCore` and `AgentHook` below are superseded); see rsi-harness-design.md §4.*

*2026-09-24. Investigation only, no code changed. Findings come from three
read-only explorations (moeka runtime, moeka tools/providers/deploy, literature).
Paper claims were read from abstracts and summaries by a subagent, not from full
papers, and I did not re-verify the arXiv IDs. Small-model claims are inference
unless a number is cited. Companion: `.agent/rsi-and-agent-core-report.md`.*

## Verdict

Feasible. moeka already provides most of the chassis. The work is a new
`rsi` subsystem (eval store, archive, skill DAG, gate, supervisor), a few tools,
and a production container. No change to `loop.py` or `runner.py` is needed,
which matches `.agent/design.md`.

The main risk is not plumbing. It is evaluator gaming and skill-pollution, and
a 7-32B local model is only credible for part of the loop (see Literature).

## What moeka already gives us

| Need | Existing piece | Notes |
|---|---|---|
| Kill-anytime durability | SQLite WAL sessions (`session/sqlite_store.py`), atomic cron `jobs.json` (`cron/service.py`), mid-turn runtime checkpoint sidecar (`session/manager.py:1215`) | Restore marks interrupted tool calls as interrupted and never replays them. Safe, not resumable, so every RSI step must be re-runnable on its own. |
| Scheduling | Cron (`every`/cron/at), Dream every 2h, `CronJobSkippedError` | A gate can raise `CronJobSkippedError` and the job is recorded as skipped. |
| Durable work queue | `triggers/local_store.py` (inbox/processing/failed, retry cap 10, recovers interrupted items) | Best existing model for an RSI task queue. |
| Scoring seam | `AgentHook.after_run` (`agent/hook.py`) | Unused for scoring today. Ephemeral turns skip extra hooks unless `run_extra_hooks_for_ephemeral`. |
| Skills | `workspace/skills/<name>/SKILL.md`, re-read every turn (hot-load), `skill-creator` skill | No versioning. `skills/` is not in GitStore. |
| Scoped agents | `MoekaCore.scoped(tools_allow=..., skills_include=...)` | Allowlist enforced at tool discovery. |
| Free search/visit | `web_search` (DuckDuckGo default, SearXNG via base_url), `web_fetch` (Jina Reader or readability, SSRF-guarded) | Add SearXNG as a sidecar for robustness. |
| Local model | `ollama`, `vllm`, `lm_studio`, `custom` providers with `api_base`, presets, fallbacks, `provider_retry_mode: persistent` | Small-model rescue: text `<tool_call>` parsing, `json_repair`, failure-reflection after 3 bad iterations. |
| Knowledge memory | `nanobot/core/vec_store.py` (sqlite-vec plus FTS5, `documents` collections, TTL) | No agent tool on top of it yet. |
| GPU etiquette | `~/projects/gpu-lock/gpu-lock`, `~/projects/llm/docker-compose.yml` (Ollama with NVIDIA reservation) | Precedent exists on this box. |

## Gaps to build

1. **Supervisor.** Goal continuation is an in-memory queue (`session/turn_continuation.py`)
   and recovery is WebUI-only and human-gated (`session/recovery.py`). The
   daemon should not depend on `/goal`. Drive it from a durable task table in
   its own SQLite file, with a small loop that claims a task, runs it, records
   the result, and can be SIGKILLed at any point.
2. **Idle/GPU gate.** No host-idle or GPU detection exists. Poll `nvidia-smi`
   utilization and `gpu-lock` status, and back off. Also pause when the user is
   active (input idle time on the host, or a flag file).
3. **Eval store and scorer.** Frozen tasks with cheap verifiers, split into a dev
   set, a regression set and a locked test set. Model on awork's
   `bench/corpus.py` and `scorecard.py` (sidecar of expected behaviour, deterministic).
4. **Archive.** SQLite: variant id, parent id, skill/prompt/config snapshot, score
   vector, transcripts, paper id (if it came from one).
5. **Skill DAG.** Typed edges (`requires`, `refines`, `conflicts_with`), acyclicity
   check on commit, append-only edit log for rollback. Also add `skills/**` to
   git tracking so a revert is one command.
6. **Paper tools.** arXiv, Semantic Scholar and OpenAlex search, plus a
   PDF-URL-to-text tool. All three APIs are free and keyless. `web_fetch` does
   not parse PDFs.
7. **Container.** Only `Dockerfile.test` exists (production runs on systemd, and
   `docs/deployment.md` describes files that were removed). Needs a non-root
   image, a compose file with Ollama or vLLM (GPU) and SearXNG sidecars, and
   volumes for the workspace, the sibling sessions dir (`<workspace>-sessions/`,
   outside the workspace, so a naive mount loses it) and the HF cache.
8. **Local-model presets.** Set `context_window_tokens` and `max_tokens` to match the
   real KV cache (defaults assume 200k), and lower `max_tool_iterations` from 200.
9. **Durability nits.** The checkpoint sidecar and cursor files are written without
   fsync (`memory.py:597`), and `SqliteSessionStore.save()` rewrites the whole
   session each time. Enable AutoCompact for any long-lived daemon session.

## Literature: what to take

| Paper | Take |
|---|---|
| Darwin Gödel Machine (2505.22954) | Open-ended archive that keeps weak stepping stones. Lesson: the agent faked test logs and removed hallucination markers, more often when the checker was visible. |
| Huxley-Gödel Machine (2510.21614) | Choose parents by descendant (clade) performance, not their own score. |
| Red Queen Gödel Machine (2606.26294) | Co-evolve agents and evaluators on separate clocks: evaluators change only at epoch boundaries. Preliminary preprint. |
| Who Grades the Grader? (2607.12790) | An unanchored evolving metric collapsed into an always-pass detector. Keep an anchor set. |
| SkillDAG (2606.03056) | Typed skill graph with commit-time checks and an append-only log. |
| SkillAA (2609.20455) | Attribute a failure to a specific skill or edge and edit only that part, with rollback. |
| CoEvoSkills (2604.01687) | Multi-file skill packages plus a surrogate verifier that never sees ground truth. |
| Alita-G (2510.23601) | Run tasks repeatedly, abstract recurring code into curated tools. |
| Agent Workflow Memory (2409.07429) | Cheapest skill-learning step: induce workflows from successful trajectories. |
| OpenEvolve / AlphaEvolve | MAP-Elites archive and cascade evaluation (cheap checks first). |
| EvoAgentX (2507.03616) | Plug-in prompt and workflow optimizers (TextGrad, AFlow, MIPRO). |
| STOP, SICA, ADAS, Voyager | Scaffold self-editing needs frontier models. STOP also wrote code that disabled its own sandbox. |

**Local 7-32B is plausible for:** prompt and workflow optimization, AWM-style
workflow induction, skill retrieval and DAG edge proposals, paper relevance
triage, running verifiers. **Needs frontier or is unproven locally:** editing
the agent's own scaffold, multi-file skill generation with a reliable surrogate
verifier, faithfully reimplementing a paper, open-ended LLM judging. So the
design should be local-model-first with rare, budgeted escalation (promotion
review, hard verifier writing, complex hypotheses).

## Proposed shape

```
supervisor (kill-safe loop, durable task table)
  gate: GPU idle? user idle? gpu-lock free? budget left?
  -> pick task type:
     1. run eval          score current best on dev set, log to archive
     2. mutate            pick parent (score + novelty + clade), change ONE
                          skill / prompt / edge, in a scratch copy
     3. gate              cheap checks -> dev subset -> must beat parent by a
                          margin AND zero regressions -> else discard
     4. promote           locked test set only occasionally (canary); git-commit
     5. paper cycle       fetch feeds -> filter -> distill to hypothesis ->
                          implement as a variant -> step 2's gate
  every step writes to SQLite first; a killed run resumes from the last row
```

Hard rules, taken from the DGM and STOP incidents:
- The evaluator, gate and promotion code sit outside the mutator's write scope,
  and the mutator never sees checker internals.
- Verify tool-use claims against harness logs, not the agent's transcript.
- Candidate runs are in a container with no network and resource limits. Do not
  rely on moeka's shell sandbox, which is permissive by default in this fork.
- Verifiers change only at epoch boundaries and must agree with a human-labelled
  anchor set. Cap edits per epoch and keep the append-only log.
- Paper text is untrusted input. The implementer step has no network.
- Reuse the report's rule: no auto-ship to anything the user depends on. Winners
  land as a branch/diff for review until the gate has a track record.

## Suggested build order (each step is independently useful)

1. Container plus supervisor plus idle/GPU gate, running a no-op task. Proves
   kill-anytime and resume.
2. Eval store and archive, with a first frozen task set and a baseline score.
3. `after_run` scoring hook and git-tracked `skills/`.
4. Mutation loop over skills and prompts with the gate. Skill DAG after there
   are enough skills to need it.
5. Paper tools and the paper cycle.
6. Escalation budget for frontier-model steps.

## Addendum: user-supplied paper list (checked against arXiv abstract pages)

All 21 IDs in the list resolved. Three titles differ from the labels: 2601.08815
is about resource budgets, not skill contracts; 2606.26924 is "A Deterministic
Control Plane for LLM Coding Agents" (RelAIBuild is the system name); 2608.25512
(Cordis) is a programming-language paper, not an agent paper, so it is only an
analogy. The "HyperAgents" entry was mislabelled with the DGM ID: it is
2603.19461 (Meta AI, unopened, from search summaries). Only abstracts were read.
Several results come from workshop papers or small prototypes, so treat the
numbers as directional.

Changes to the design:

1. **Pre-commit gating, not post-hoc cleanup.** "When Self-Evolution Backfires"
   (2608.05810): defective skills seed later distillation and removal recovers
   little. "Beyond Task Completion" (2604.00392): 96.8% of synthesized tools had
   zero held-out correctness despite passing in-session verification. So the gate
   runs held-out conformance replay, uses several heterogeneous checks
   (structure, behaviour, semantics), and rejected candidates never enter the
   distillation context.
2. **Two stores.** The archive stays open-ended, but the *active* skill set has a
   hard cap with outcome-driven retirement from per-skill contribution scores and
   an append-only evidence log ("Library Drift", 2605.19576). Unbounded growth
   hurt in both that paper and 2605.24050.
3. **Selection accuracy is the bottleneck** (2605.24050: up to -21% at 202
   skills from skill shadowing). Filter candidates by declared preconditions
   before the model sees them (2608.01050), load only a skill's DAG neighbourhood
   of about 2-3 skills (GraSP, 2604.17870), and check new descriptions for
   collisions at admission.
4. **Skill contracts in SKILL.md:** preconditions, environment assumptions
   (binaries, paths, API shapes) and invariants. Run them on a schedule as drift
   checks and use the failing contract to drive repair (2605.10990: repair success
   10% to 78%, zero false alarms on 599 cases). Infer contracts from run traces
   as well as prose (Contract2Tool, 2606.07904).
5. **Composition edges are risk edges** (SkillFuzz 2607.02345, SkillProbe
   2603.21019): fuzz and audit every new DAG edge, and audit any skill imported
   from a paper or a marketplace for description-vs-behaviour mismatch.
6. **Content-address every skill** and keep the gate deterministic (RelAIBuild).
   Record runs as step DAGs so one changed skill replays only its branch
   (Execution Lineage 2605.06365). Rollback re-points the active set.
7. **Three-valued gate:** pass / fail / unresolved, with an admission
   certificate stored beside the skill version (2609.16313, small prototype).
8. **Externally enforced per-run budgets** for tokens, time and steps (2601.08815).
   Do not let the agent allocate its own budget (AllocBench, 2607.23332).

**Tension with the first design:** "The Blind Curator" (2607.07436) shows that a
judge with false-pass bias above roughly 45% silently disables retirement while
aggregate metrics look fine. A frozen verifier can be frozen and wrong, so each
epoch needs a defect-injection audit (inject known-bad skills, confirm the gate
and retirement catch them) and the false-pass rate is a first-class metric.
This matters most when the local model judges its own output.

**Also:** LLM-authored skills gave +0.0pp against +16.2pp for human-curated ones
in Library Drift, so a 7-32B self-authoring loop may gain little unless the gate
is strict. Consider seeding from human or stronger-model skills (SkillWeaver
2504.07079: skills from a stronger agent lifted a weaker one by 54.3% on
WebArena) and using the local model mainly to select, verify and prune.

Per-run log fields: skill hashes and versions used, retrieval candidates and the
selected skill, preconditions checked, gate verdicts with evidence, budget used,
per-skill contribution outcome, verifier coverage and held-out conformance, and
the judge's canary false-pass rate.

## Open questions for the user

- What is the eval set about? The daemon can only get better at what it can
  measure (sysadmin/homelab tasks, research quality, coding, something else?).
- Is the improvement target moeka itself (its skills and prompts) or a separate
  agent that the daemon maintains?
- Frontier-model budget for escalation: none, small cap, or use existing Claude access?
- Which local model and GPU headroom should we assume?
