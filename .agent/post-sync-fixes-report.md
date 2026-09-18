# Post-sync fixes report — 2026-09-18

Three owner decisions, addressed in order, after all 7 batches of the
upstream-main sync (`.agent/upstream-sync-plan.md`) landed and were
independently verified. Worktree `/home/muk/projects/moeka-sync`, branch
`merge/upstream-main-2026-08-05`. Nothing pushed. `/home/muk/projects/moeka`
confirmed on `main`, untouched, throughout.

## 1. Environment-dependent tests skip, not fail — all four categories

Built one shared, session-scoped capability probe module,
`tests/_capabilities.py` (a plain module, not a conftest — ships to Docker
automatically via the existing `COPY tests/ tests/` in `Dockerfile.test`,
no Dockerfile change needed, following the same reasoning as
`tests/_home_guard.py`):

- `has_dns_egress(hostname="example.com")` — a real `socket.getaddrinfo`
  probe, `lru_cache`d for the session (a DNS lookup has real latency and
  the environment's capability doesn't change mid-run).
- `has_npx()` — `shutil.which("npx") is not None`, also cached.

Both are capability checks, not hostname/platform/environment-name guards
— the same test runs for real wherever the capability exists (a real
network sandbox, a CI image with Node installed) and skips, with a reason
string naming exactly what's missing, everywhere it doesn't. No `xfail`
used anywhere, per the explicit instruction — a skip with a reason is
honest, an xfail quietly tolerates breakage.

Applied to all four failure categories:

1. **`tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls`**
   — 2 of the 3 parametrized commands (`wget ... example.com`,
   `python3 ... urlopen('http://example.com')`) now carry
   `marks=pytest.mark.skipif(not has_dns_egress(), reason=DNS_EGRESS_SKIP_REASON)`.
   The third (`curl ... https://www.google.com`) is left unconditional — it
   was never in the known-failing set; `_guard_command`'s
   `trust_remote_dns` handling doesn't require a successful resolution for
   that call shape, so it isn't actually DNS-dependent. Verified this by
   running the file in Docker before touching it, not by assumption.
2. **`tests/webui/test_mcp_presets_api.py`** — both
   `test_test_mcp_preset_connects_and_reports_tools` and
   `test_test_mcp_preset_inspects_tools_outside_the_enabled_allowlist` now
   carry `@pytest.mark.skipif(not has_npx(), reason=NPX_SKIP_REASON)`.
   Traced the actual root cause first rather than trusting the earlier
   label: both tests mock `connect_mcp_servers`, but
   `mcp_presets_test_action()` calls `_command_available(cfg.command)`
   (`cfg.command == "npx"` for the playwright preset) *before* ever
   reaching the mock — confirmed via `nanobot/webui/mcp_presets_api.py`'s
   `mcp_presets_test_action`.
3. **`nanobot/channels/dingtalk/tests/test_dingtalk_channel.py`** — the 8
   SSRF-redirect tests exposed by batch 7's `Dockerfile.test` change
   (installing all channel dependencies, which made this previously
   `importorskip`-skipped module load for the first time) now each carry
   `@pytest.mark.skipif(not has_dns_egress(), reason=DNS_EGRESS_SKIP_REASON)`
   above their existing `@pytest.mark.asyncio`. Re-confirmed via a targeted
   Docker run that every one of the 8 failures traced to
   `reason=Cannot resolve hostname: example.com` in the runtime's own log
   output before marking them — same root cause as category 1, not a code
   defect.

**Verification**: targeted Docker run of the three affected files —
`134 passed, 12 skipped`, each skip reported with its capability reason
(`requires DNS/network egress, not available in this environment` /
`requires npx on PATH, not available in this environment`). Full suite
confirms below.

## 2. Restored moeka's cron "only persist when a job ran" fix

`558fe35e`'s fix had been dropped silently during batch 3 — upstream's
`_on_timer` (the tick handler, now considerably restructured from the
original flat `tick()` this fix targeted — it now tracks
`_active_executions`/`_store_dirty` and re-fetches each candidate job via
`self.get_job(candidate.id)` before executing, to handle jobs
deleting/disabling/rescheduling each other mid-tick) had gone back to
calling `self._save_store()` unconditionally after the due-jobs loop,
rewriting `jobs.json` on every tick (default every 5 minutes) even when
nothing ran.

Restored, adapted to the current shape (`nanobot/cron/service.py`,
`_on_timer`): track `executed_any` across the re-fetch loop rather than
just checking whether the initial `due_jobs` list was non-empty — this is
slightly more precise than the original fix and correctly handles the case
upstream's restructuring introduced that didn't exist when `558fe35e` was
written: every initially-due candidate getting skipped in the re-fetch
(deleted/disabled/rescheduled by an earlier job in the same tick), where
`due_jobs` alone being non-empty would still have triggered a save under a
naive port of the old logic.

```python
if executed_any:
    self._save_store()
```

Did not touch `_store_dirty`'s own crash-recovery save at the top of
`_on_timer` (line ~610) — that's a different concern (persisting a prior
tick's already-advanced-but-unpersisted state after a write failure), not
what this fix is about, and restoring it correctly required leaving that
path alone.

**Regression tests** added to `tests/cron/test_cron_service.py`:
- `test_idle_tick_does_not_rewrite_the_store` — adds a job with
  `next_run_at_ms` in the future, spies on `_save_store`, asserts it is
  never called on a tick with nothing due, and asserts `jobs.json`'s mtime
  is byte-for-byte unchanged.
- `test_tick_with_a_due_job_still_saves` — the counterpart: a due job must
  still be persisted, so the fix doesn't overshoot into never saving.

Both new tests, plus the existing 58 in that file, pass (60/60 total).

## 3. Runner-seam assessment (analysis only, not implemented)

Delegated to a dedicated read-only assessment task, mirroring the rigor of
the earlier session-store seam decision (`.agent/plugin-seam-assessment.md`).
Written to **`.agent/runner-seam-assessment.md`** (285 lines). Not
implemented, per instructions — no code changed for this item.

Headline findings, for a quick read (the file has the full line-cited
detail):

- **`execute_tool_calls()` (upstream's extracted function) is dead code in
  moeka's actual runtime today** — every call site found via a repo-wide
  grep is a test file, not `runner.py`, `subagent.py`, or any production
  tool path. This reframes the whole question: there is currently zero
  risk of the two paths silently disagreeing in production, because only
  one of them runs. The cost of the fork is pure "manually re-apply
  upstream commits that touch `execution.py`" labor, not a live
  correctness hazard.
- **`exec_guard_denial` is trivially portable** — a third branch in
  `_classify_violation`, no change to upstream's function needed, if the
  decision were ever made to converge.
- **`AskUserInterrupt` genuinely cannot be accommodated by a hook or
  parameter alone** — it needs upstream's return-contract/control-flow to
  change (it's deliberately a `BaseException` subclass specifically so it
  isn't caught by `execution.py`'s `except Exception`, meaning if
  `execute_tool_calls()` were ever wired into production unmodified as-is,
  an `ask_user` call would propagate uncaught through `asyncio.gather()`
  and cancel sibling concurrent tool calls — a genuine latent bug the
  assessment surfaced while comparing exception handling, not something
  it went looking for).
- **Recommendation**: a small, concrete return-contract change to
  `execute_tool_calls()` (exact edit points cited in the doc) would let it
  converge, honestly estimated at roughly half a day to a day of work.
  Until/unless that's actually done, keep the fork, but add the
  parity/taxonomy tests the doc describes (asserting the fork and
  `execution.py` agree on some invariant — same exception types handled,
  same tool-result shape) so future drift is caught by a failing test
  instead of discovered by hand during the next merge batch, the way it's
  happened 4 batches running.

## Docker numbers (authoritative, `scripts/test-docker.sh`, full suite)

```
7959 passed, 0 failed, 68 skipped, ~270s
```

**0 failures**, as required. Up from batch 7's `7957 passed, 12 failed, 56
skipped` — the 12 that were failing are now correctly counted among the 68
skips (56 + 12 = 68), each with a capability-naming reason, not silently
dropped or papered over.

## Verification

```
git diff 2fb16593988b9e85131e02f395bb9a5108e220e7 -- nanobot/session/manager.py
```
→ empty (untouched by this pass — none of the three items touch it).

```
git -C /home/muk/projects/moeka branch --show-current
```
→ `main`, confirmed before and after. `~/.nanobot`/`~/.nanobot-sessions`
were not touched (no test isolation guard changes were needed for this
pass; the existing guards from the earlier review-fixes pass remain in
place and were exercised, not modified, by this session's test runs).

`ruff check` clean on every file touched (`nanobot/cron/service.py`,
`tests/cron/test_cron_service.py`, `tests/_capabilities.py`,
`tests/tools/test_tool_validation.py`, `tests/webui/test_mcp_presets_api.py`,
`nanobot/channels/dingtalk/tests/test_dingtalk_channel.py`).

## Files touched

- `tests/_capabilities.py` (new) — shared DNS-egress/npx capability probes.
- `tests/tools/test_tool_validation.py` — 2 of 3 parametrized cases
  gated on `has_dns_egress()`.
- `tests/webui/test_mcp_presets_api.py` — 2 tests gated on `has_npx()`.
- `nanobot/channels/dingtalk/tests/test_dingtalk_channel.py` — 8 tests
  gated on `has_dns_egress()`.
- `nanobot/cron/service.py` — `_on_timer` only calls `_save_store()` when
  a job actually executed this tick (restores `558fe35e`'s intent,
  adapted to the current `_active_executions`/candidate-refetch shape).
- `tests/cron/test_cron_service.py` — 2 new regression tests.
- `.agent/runner-seam-assessment.md` (new) — analysis only, not acted on.
