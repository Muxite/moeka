# Runner tool-execution seam assessment — `_execute_tools`/`_run_tool` vs `execute_tool_calls()`

Read-only analysis, requested after Batch 7 of the 2026-08-05 upstream-main
sync, in response to the recurring cost flagged in `.agent/upstream-sync-notes.md`
(Batch 5 §"tool-execution-boundary decision", Batch 6 "duplication-drift"
open item, Batch 7's largest port yet). No code changed for this. Same
spirit/rigor as `.agent/plugin-seam-assessment.md`'s session-store seam
analysis — concrete extension-point check first, verdict second.

Files read in full: `nanobot/agent/runner.py` (1811 lines, focused on
`_execute_tools`/`_run_tool`/`_classify_violation` and everything upstream/
downstream of them) and `nanobot/agent/tools/execution.py` (316 lines, all
of it).

## 0. A fact that changes the framing: `execute_tool_calls()` is dead code in production

```
$ grep -rn "execute_tool_calls\b" nanobot/ tests/ | grep -v "def execute_tool_calls"
tests/agent/test_runner_errors.py
tests/agent/test_runner_file_reads.py
tests/agent/test_runner_tool_execution.py
tests/agent/tools/test_subagent_tools.py
```

Every call site is a test. Nothing in `nanobot/` — not `runner.py`, not
`subagent.py`, not any tool — calls `execute_tool_calls()`. It exists
because upstream extracted it from their own runner and upstream's own
tests (`tests/agent/test_runner_*.py`, `tests/agent/tools/test_subagent_tools.py`)
exercise it directly. moeka kept it un-deleted (correctly — deleting an
upstream module invites a delete/modify conflict every batch) but never
wired it in. So today there is **zero runtime risk** from the fork: the
duplication cost is 100% "manually re-apply upstream commits that touch
`execution.py` into `runner.py`'s copy," not "these two code paths can
silently disagree about what actually runs." That reframes the decision
from a correctness question to a pure labor/maintainability question.

## 1. `execute_tool_calls()`'s actual signature and extension points

```python
async def execute_tool_calls(
    tools: ToolRegistry,
    tool_calls: list[ToolCallRequest],
    *,
    concurrent: bool,
    external_lookup_counts: dict[str, int],
    workspace_violation_counts: dict[str, int],
    hook: AgentHook,
    context: AgentHookContext,
    model_messages: list[dict[str, Any]] | None = None,
    compacted_tool_results: set[str] | None = None,
) -> tuple[list[Any], list[dict[str, str]]]
```

Existing extension points: `hook: AgentHook` (`before_execute_tool`,
`after_execute_tool`, `on_execute_tool_error`, all called per-tool-call —
see `nanobot/agent/hook.py:66` on) and `context: AgentHookContext`. These
are observation hooks only — none of them can change the return value,
short-circuit the batch, or turn a single tool call's outcome into "stop
processing the rest of this batch and return control to the caller."
There's no callback for "classify this failure text specially" and no way
to inject a pluggable classifier. The function returns a flat
`tuple[list[Any], list[dict[str, str]]]` — no per-call exception object
survives past the `except Exception as exc:` handlers at
`execution.py:177`/`196`; everything becomes payload text + an event dict.

`compacted_tool_results`/`model_messages`/the `@cache`-wrapped `read_results()`
closure (`execution.py:69-79`) is the file-read-dedup mechanism ported in
Batch 7 — this one *did* get cleanly absorbed as new optional kwargs, and
runner.py's fork now genuinely mirrors it (`runner.py:1466-1482`,
`1538`, `1585-1588`). That is the one piece of evidence that upstream's
shape *can* accept new optional parameters without a redesign, when the
feature is "index some extra read-only context." Both of moeka's needs are
a different kind of feature — see below.

## 2. Cataloging every real behavioral difference

Confirmed against both full files, not just the notes:

| Feature | `execution.py` | `runner.py`'s fork | Verified independently? |
|---|---|---|---|
| Return shape | `tuple[list[Any], list[dict]]` (2-tuple) | `tuple[list[Any], list[dict], BaseException \| None]` (3-tuple) | Yes — `execution.py:67`, `_run_tool` at `runner.py:1539` |
| `AskUserInterrupt` handling | None. `except Exception as exc:` at `execution.py:177` treats `AskUserInterrupt` (a `BaseException` subclass, `nanobot/agent/tools/ask.py:12`) — actually it wouldn't even be caught, since `AskUserInterrupt(BaseException)` is *not* an `Exception` subclass, so it would propagate straight out of `execute_tool_calls()` as an uncaught exception through `asyncio.gather` | Explicit: `_run_tool` special-cases `isinstance(exc, AskUserInterrupt)` at `runner.py:1602-1604`, returns `("", event, exc)` without re-raising; `_execute_tools` stops the current batch early on it (`runner.py:1515-1518`) so later tool calls in the same model turn are never started, then returns the completed prefix's results plus the interrupt as `fatal_error` | Yes, re-derived independently, not just copied from the sync notes |
| Batch early-stop on interrupt | None — `asyncio.gather` runs the whole batch to completion (or the interrupt propagates and kills everything, including sibling concurrent calls, with no partial results preserved) | `_execute_tools` breaks out of the `for batch in batches:` loop as soon as any result in the just-run batch carries an `AskUserInterrupt` (`runner.py:1515-1518`), preserving every already-completed result/event pair before the interrupt | Yes |
| `exec_guard_denial` classification | Not present at all — `_classify_violation` in `execution.py:244` only recognizes SSRF and generic workspace-violation markers | `_classify_violation` at `runner.py:1688` has a third branch: `exec_guard_violation_signature(raw_text)` (allowlist/deny-pattern denial text), escalating via `repeated_exec_guard_error`, with its own event detail tags (`exec_guard_denial:`/`exec_guard_denial_escalated:`) and — importantly — it returns the raw denial text **unmodified**, explicitly skipping the generic `hint` append (`runner.py:1723-1726`: "the generic 'try a different approach' retry hint... contradicts the guard's 'retrying will not help' instruction") | Yes |
| `file_read_context`/read-dedup | Present (Batch-7-era upstream feature) | Present, ported in Batch 7, verified 16/16 against upstream's own test | Yes, both sides match |
| `_partition_tool_batches` (concurrency-safe batching) | Present, module-level function taking `tools`/`tool_calls`/`concurrent` | Present, `self._partition_tool_batches(spec, tool_calls)` — logic is otherwise identical statement-for-statement | Yes — genuinely equivalent, this part isn't actually forked in spirit, just re-hosted as a method |
| SSRF / generic workspace-violation classification | Present, `_classify_violation`/`_is_ssrf_violation`/`_is_workspace_violation`/`_ssrf_soft_payload`/`_event_detail` | Present, same logic, same constant lists (`_SSRF_MARKERS`, `_WORKSPACE_VIOLATION_MARKERS`), re-hosted as classmethods/staticmethods | Yes, byte-for-byte equivalent markers |
| `repeated_external_lookup_error`/`repeated_workspace_violation_error`/`exec_guard_violation_signature`/`repeated_exec_guard_error` | Imported from shared `nanobot/utils/runtime.py` | Same shared import (`runner.py:59-70`) | Yes — these are **not** forked; only the call site (`_classify_violation`) differs by having one extra branch |

**Nothing found that upstream's `execution.py` has and moeka's fork is
now silently missing.** Batch 7's file-read-dedup port was verified
against upstream's own test suite (16/16), and the read of both files
here confirms the two `_classify_violation`/`_execute_tool_call` bodies
are otherwise structurally identical line-for-line (same variable names,
same control flow, same helper decomposition) — moeka's fork reads like a
disciplined manual mirror, not an independently-evolved implementation.
That's good news for drift risk (a future diff would show exactly two
localized deltas) and bad news for why it's still worth asking "why
maintain two mirrors instead of one."

## 3. Can `execute_tool_calls()` be extended to cover both needs without upstream changing its own design?

**`exec_guard_denial`: yes, cleanly.** This is genuinely just a third
`if` branch in `_classify_violation`'s equivalent, using already-shared
`nanobot/utils/runtime.py` functions upstream doesn't call but doesn't
own either — they're already moeka-added utilities sitting next to
upstream's own `repeated_external_lookup_error`/`repeated_workspace_violation_error`.
Concretely: add the exact block currently at `runner.py:1707-1726` into
`execution.py`'s `_classify_violation` (`execution.py:244-280`), between
the SSRF check and the generic workspace-violation check. Zero signature
changes required — `_classify_violation` already receives `raw_text`,
`event`, `tool_call`, `workspace_violation_counts`, which is everything
the branch needs. This is a pure upstream-neutral addition: upstream
doesn't have `exec_guard_violation_signature` concept at all today, so
adding it either (a) requires a small upstream PR, or (b) can live as a
moeka-only patch to `execution.py` if moeka is willing to carry *that*
one function's diff instead of the whole `_execute_tools`/`_run_tool`
pair. Either way this alone does not justify keeping two ~150-line
mirrors.

**`AskUserInterrupt`: no, not via a parameter or callback — this is the
real structural blocker.** The reason is not "the classification logic is
shaped wrong," it's that **`execute_tool_calls()` has no yield-back point
mid-batch.** Its contract is: hand it every tool call from one model
turn, get back full-length parallel `results`/`events` lists once
everything has run (or raised uncaught). There is nowhere in that contract
to say "stop after N of the M calls in this concurrent `asyncio.gather()`
batch, keep the N results, and tell the caller specifically *why* it
stopped so the caller can suppress the interrupted call's own result
message and pivot to `stop_reason = "ask_user"`." Concretely:

- `asyncio.gather()` at `execution.py:83-94` runs a whole concurrent batch
  to completion or aborts entirely on an unhandled exception — it cannot
  return a partial-batch prefix. moeka's version handles this by *not*
  using `asyncio.gather` across an interrupt boundary the same way:
  `_execute_tools` checks `isinstance(result[2], AskUserInterrupt)` after
  each batch and breaks the outer `for batch in batches:` loop
  (`runner.py:1515-1518`), which only works because the 3-tuple carries
  the interrupt object out of `_run_tool` instead of raising it.
- Even granting a callback/hook that could "signal stop" mid-gather,
  `execute_tool_calls()`'s 2-tuple return has no slot to carry "why did we
  stop, and is it recoverable" back to the caller — the caller (`runner.py`'s
  main loop, `runner.py:570-631`) needs to distinguish "a tool raised a
  real error, abort the turn with `stop_reason = tool_error`" from "the
  model asked the user something, pause with `stop_reason = ask_user`, and
  resume later by re-entering with the user's answer appended as the tool
  result for that specific `tool_call_id`" (see `ask_user_tool_result_messages`
  in `nanobot/agent/tools/ask.py:96-108`, and `pending_ask_user_id` used on
  the resume path). A 2-tuple genuinely cannot express that distinction;
  it would need to become at least a 3-tuple (which is exactly moeka's
  fork) or an explicit result object/enum.
- `AskUserInterrupt` is `BaseException`, not `Exception`, specifically so
  it is *not* caught by `execution.py`'s `except Exception as exc:` at
  line 177 — today it would propagate straight through
  `execute_tool_calls()` uncaught, which in a concurrent `asyncio.gather()`
  batch also cancels and discards the sibling calls' results. Extending
  `execute_tool_calls()` to handle this "correctly" (i.e. the way
  moeka's fork does) is not a bolt-on parameter; it requires touching the
  `except` clause, the batch-loop control flow, and the return type — the
  three things a caller-supplied hook/callback cannot reach into from the
  outside, because Python doesn't let an injected callback abort a
  `try/except` block or truncate a `for` loop it isn't itself running.

So: one of the two needs (`exec_guard_denial`) is a trivial upstream-shaped
addition; the other (`AskUserInterrupt`) requires upstream's own function
to change its return contract and internal control flow, which is exactly
the "upstream itself has to change its design" case the task asked me to
distinguish from "moeka can extend from the outside."

## 4. Recommendation: **converge is possible, but only via an upstream-side signature change — not via extension from moeka's side alone.** Net: propose the change upstream; if it's not accepted or not timely, keep the fork with a drift detector.

Concretely, if pursuing convergence:

1. **Upstream (or moeka-forked-in-execution.py) change**: give
   `execute_tool_calls()` an optional 3-tuple mode. Cleanest shape,
   staying close to what's already there:
   ```python
   async def execute_tool_calls(
       ...,
       ask_user_aware: bool = False,
   ) -> tuple[list[Any], list[dict[str, str]]] | tuple[list[Any], list[dict[str, str]], BaseException | None]:
   ```
   or, less magic-typed and easier to keep a stable call signature: always
   return the 3-tuple and have callers that don't care simply ignore the
   third element (this is what moeka would actually want, and is a
   smaller diff against `execution.py` than a flag-gated return-type
   union). This requires: (a) moving the `AskUserInterrupt` catch above/
   alongside the generic `except Exception` in `_execute_tool_call`
   (`execution.py:177`), since `BaseException` subclasses aren't caught by
   `except Exception`; (b) changing `_partition_tool_batches`'s consumer
   loop in `execute_tool_calls` to check the third tuple element and
   `break` early the same way `runner.py:1515-1518` does; (c) adding the
   `exec_guard_denial` branch from §3 to `_classify_violation`.
2. **moeka-side call site after conversion**: `runner.py:570` would call
   `execute_tool_calls(spec.tools, tool_calls, concurrent=spec.concurrent_tools, ...)`
   directly, and `_execute_tools`/`_run_tool`/`_classify_violation`/
   `_partition_tool_batches`/the SSRF and workspace-violation
   classmethods (`runner.py:1458-1811`, ~350 lines) would all delete.
3. **Test impact**: `tests/agent/test_runner_errors.py`,
   `test_runner_file_reads.py`, `test_runner_tool_execution.py`,
   `tests/agent/tools/test_subagent_tools.py` already exercise
   `execute_tool_calls()` directly and would need new
   `AskUserInterrupt`/`exec_guard_denial` cases added (currently these
   behaviors are presumably tested, if at all, only through `runner.py`'s
   own test suite calling the full `AgentRunner.run()` path — worth
   confirming exact coverage before starting; not verified in this pass
   since it's out of scope for a read-only assessment focused on the two
   source files named in the task). Any moeka test currently calling
   `runner._execute_tools`/`_run_tool` directly (if such tests exist)
   would need to be repointed at the module-level function.
4. **Honest cost estimate — convergence path**: 0.5–1 day of implementation
   (the diff itself is small and mechanical, per §3's exact line
   citations) **plus** the real cost, which is not implementation but
   *upstream coordination*: this is a public-repo function with its own
   test suite; changing its return contract is a compatibility break for
   any other consumer of `execution.py` (none exist in moeka today, but
   upstream may have other callers moeka hasn't tracked, or plans to add
   some). If moeka forks `execution.py` itself instead of proposing this
   upstream, that just moves the "two files to keep in sync" problem from
   `runner.py` vs `execution.py` to `execution.py` (moeka's) vs
   `execution.py` (upstream's) — same category of recurring merge cost,
   not eliminated. If proposed upstream and accepted, the payoff is real:
   every future `execution.py`-only upstream commit (like Batch 5's
   `919e3d34`) becomes a clean merge instead of a manual port, because
   `runner.py` would just call the shared function.

**If convergence isn't pursued (upstream doesn't want the return-contract
change, or there's no appetite to propose/wait on it), keep the fork — but
close the loop with an actual drift detector**, since "flagged as an open
concern" (Batch 6) with no automated check is exactly how a 4th, 5th, 6th
batch of silent drift happens. Concrete proposal, cheap to build:

- A test (e.g. `tests/agent/test_runner_execution_parity.py`) that
  imports both `nanobot.agent.tools.execution._classify_violation` and
  a small wrapper extracting the equivalent logic from
  `AgentRunner._classify_violation`, and asserts on a **shared table of
  (raw_text, expected_classification) fixtures** — SSRF text, workspace
  text, exec-guard text, unrecognized text — that both implementations
  return the same classification *category* (ignoring the one
  intentional extra `exec_guard_denial` branch, which the test explicitly
  allowlists as "moeka-only, expected"). Any new marker/branch added to
  one side and not the other fails the test immediately instead of
  waiting for the next sync batch's manual side-by-side read.
- A second, narrower test asserting the two `_execute_tool_call`/
  `_run_tool` bodies handle the same **exception taxonomy** —
  specifically that both correctly special-case `AskUserInterrupt` (today
  `execution.py` does NOT — this would actually catch the real bug that
  `AskUserInterrupt` silently escapes `execute_tool_calls()`'s `except
  Exception` today) and that both apply `_with_retry_hint`/`hint` under
  the same conditions.
- Cheapest version if a full parity harness is too much: a single
  focused regression test asserting `execution.py`'s `except Exception`
  clause does *not* catch `BaseException` subclasses (documenting today's
  actual gap), plus a code comment/CI grep step (already informally done
  via the `runner.py:564-569` comment block) that fails CI if
  `execution.py`'s line count or its `_classify_violation` function body
  changes without a corresponding diff to `runner.py`'s copy — a coarse
  tripwire, not a semantic check, but enough to force a human look instead
  of a silent 4-batches-deep drift.
- **Honest cost estimate — keep-the-fork-with-detector path**: 2-4 hours
  for the fixture-table parity test (the SSRF/workspace-violation logic
  is already byte-identical, so the fixtures are easy to write and should
  pass immediately); the `AskUserInterrupt`-taxonomy test is a genuine
  bug-catcher worth adding regardless of which path is chosen, since it
  currently documents a real difference in exception-handling breadth
  between the two files that nothing today asserts on.

## Bottom line

Convergence is technically possible and the mechanical diff is small
(~half a day), but it requires upstream's `execute_tool_calls()` itself to
change its return contract and exception handling — not something moeka
can bolt on from the caller side with a parameter or hook, because the
missing piece is a mid-batch yield-back point that the function's current
`asyncio.gather()`-to-completion, 2-tuple-return shape structurally
doesn't have. The `exec_guard_denial` half of the fork, taken alone, could
converge trivially and isn't a good reason by itself to keep the
duplication. Given that `execute_tool_calls()` is unreferenced in
production code today (§0) and the two implementations were re-verified
here to be otherwise line-for-line identical (not independently drifted
in spirit, "just" un-synced in practice), the pragmatic move is: propose
the return-contract change upstream once (cheap to write, given §3's
exact edit points), and in the meantime add the parity/taxonomy tests in
§4 so the next sync batch gets an automated flag instead of a 5th manual
side-by-side read.
