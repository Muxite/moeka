# Upstream sync plan — 2026-09-16

## Survey summary

- `main` is the de-facto integration tip: it already absorbed
  `merge/upstream-main-2026-07-28` and `merge/upstream-main-2026-08-03`
  (commit `2b19da40`, 2026-08-03). Both those integration branches are fully
  merged (`git log merge/...^..upstream/main` shows 744 unmerged commits
  remaining — same gap as `main`), i.e. they are stale/superseded, not
  "in-flight". Treat them as closed.
- `nightly` is **not** the active line right now — it stopped at 2026-05-17
  and is 20 commits behind `main` (real post-merge fixes landed on `main`
  directly, bypassing `nightly`, contrary to CLAUDE.md's documented
  strategy). It is also 1951 commits behind `upstream/main` and carries 43
  commits `upstream/main` doesn't have. `nightly` being stale is a process
  finding, not something this task fixes — flagging it for the owner.
- `feat/sqlite-vec-memory` is fully merged into `main` (0 commits ahead) —
  irrelevant, ignore.
- Working tree is clean, `main` is checked out but untouched by this task
  (we only ever branch off it, never commit to it).
- **Gap to close: `main..upstream/main` = 744 commits, 2026-07-28 →
  2026-09-16 (~7 weeks), 1165 files changed, +197725/-51116 lines.**
  Heaviest churn: `nanobot/channels` (251 files), `webui/src` (205 files,
  +68498/-23229 alone), `tui/src` (48 files, new-ish TUI surface upstream
  keeps building out — small in this window, +242/-4), `tests/agent` (75),
  `nanobot/agent` (45), `nanobot/webui` (44), session subsystem changes
  (`session_messages.py`, `summary.py`, `turn_continuation.py`,
  `webui_turns.py` — 10 files, 27 commits touch `session/manager.py`).
  `nanobot/core` (moeka's own plugin/RAG core) has **zero** upstream
  commits touching it in this window — good, low conflict risk there.
  `tools/registry.py` (plugin loader) has 5 upstream commits;
  `tools/shell.py` has 14.

This is large (744 commits) but not as extreme as the raw `nightly` gap
(1951). Basing off `main` instead of stale `nightly` roughly halves the
work and matches the "everything is a plugin abstraction, memory is
smarter" framing: `main` already contains the layer that matters.

## Batching strategy

Base branch: `main` tip (`2b19da40`) — branch off it, never commit to it.
Batch by upstream calendar week (Tuesday-anchored checkpoints, matching
upstream's own now-abandoned `nightly-26-*` tagging cadence):

| # | Upstream checkpoint (date, sha)              | Commits from main | Cumulative |
|---|-----------------------------------------------|--------------------|------------|
| 1 | 2026-08-05  `67805f5d`                         | 134                | 134        |
| 2 | 2026-08-12  `0c684c5a`                          | 102                | 236        |
| 3 | 2026-08-19  `1018bdb7`                          | 167                | 403        |
| 4 | 2026-08-26  `f9d449ef`                          | 92                 | 495        |
| 5 | 2026-09-02  `d81aa5a4`                          | 76                 | 571        |
| 6 | 2026-09-09  `20f115bf`                          | 87                 | 658        |
| 7 | 2026-09-16  `2fb16593` (upstream/main tip)      | 86                 | 744        |

Each batch = one `git merge --no-ff <checkpoint-sha>` into a rolling
integration branch (`merge/upstream-main-<checkpoint-date>`), one merge
commit per batch (not squashed), so conflict resolution stays reviewable
and bisectable. If a batch's conflict set is unexpectedly large, split it
further by sub-date rather than force through.

## Expected conflict surfaces vs. the two things that matter

Per the reframed identity ("moeka = nanobot + plugin abstraction + smarter
memory"), conflicts are triaged as:

**Plugin abstraction** (`nanobot/agent/tools/registry.py`,
`nanobot/agent/tools/*` auto-loader, `nanobot/core/function_tool.py`,
`bg_shell` gating, `tools.exec.allowPatterns`):
- `tools/registry.py` — 5 upstream commits. Expect loader/discovery
  refactors; keep moeka's plugin-abstraction shape, port upstream's new
  tool registrations/behavior into it.
- `tools/shell.py` — 14 upstream commits, including
  `upstream/fix/shell-deny-pattern-scoping` (a branch specifically about
  deny-pattern scoping — high risk of reintroducing destructive-command
  deny patterns or touching `allow_sudo`). **This is the single highest
  scrutiny item.** Take upstream's scoping *fix logic* but re-assert
  moeka's permissive default deny list and `allow_sudo=False` on top.
- `bg_shell` — verify `enabled(ctx)` still returns False after merge.

**Memory** (`nanobot/session/manager.py`, `session/*`, `nanobot/core/vec*.py`,
Dream consolidation in `nanobot/agent/memory.py`):
- `session/manager.py` — 27 upstream commits. Highest-churn memory file.
  Expect TTL/compaction/session-messages refactors
  (`session_messages.py`, `summary.py`, `turn_continuation.py`,
  `webui_turns.py` are new/changed upstream files in this window — these
  look like upstream splitting session concerns into modules). Must keep:
  SQLite `sessions.db` WAL store, cross-process SQLite locking, legacy
  jsonl one-time import (newer-wins, rename to `*.jsonl.imported`),
  `dump_jsonl`, and the "never import global legacy sessions dir into
  non-primary workspaces" fix (70d9d981). Port upstream's new
  session-message/summary/turn-continuation logic *into* the SQLite
  store shape rather than adopting any upstream per-file/jsonl persistence
  change wholesale.
- `nanobot/core/vec*.py` — moeka-only (0 upstream commits touch
  `nanobot/core` in this window) — no conflict expected, but re-verify
  after each batch that nothing upstream reintroduces a competing vector
  memory path that should instead defer to moeka's `VecStore`.

**Not plugin/memory — prefer upstream, drop local divergence, note it**:
webui (`webui/src`, `nanobot/webui`), `tui/src`, channels (`nanobot/channels`
— 251 files, mostly WhatsApp/Feishu/etc. feature churn), providers, CLI,
docs. Where moeka has small incidental diffs here (e.g. Telegram
`drop_pending_updates`, transcription `api_base`), keep the moeka behavior
only if it's a deliberate runtime-safety default; otherwise take upstream
and note the drop.

**Always keep regardless of category** (branding/docs/launchers, not a
"concept", just don't want them back): no `CONTRIBUTING.md`, no
`images/nanobot_logo.png`, moeka's `CLAUDE.md`, `.agent/*`, `bin/*`
launchers.

## Verification per batch

- `ruff check nanobot/` (never `ruff format`).
- `scripts/test-docker.sh` full suite; record pass/fail counts.
- Targeted smoke greps after each batch:
  - `grep -n "allow_sudo" nanobot/agent/tools/shell.py` → still defaults False
  - confirm `rm -rf`/`dd`/`mkfs`/`format`/`shutdown` absent from deny lists
  - `grep -n "sessions.db" nanobot/session/manager.py`
  - `grep -n "_dispatch_with_watchdog" nanobot/channels/manager.py`
  - `grep -n "enabled" nanobot/agent/tools/bg_shell*.py` (or wherever bg_shell lives) → returns False
  - `test ! -f CONTRIBUTING.md && test ! -f images/nanobot_logo.png`
  - `grep -rn "drop_pending_updates" nanobot/channels/telegram*` → default True
- After batch 7 (final), diff `nanobot/core/` against pre-merge `main` to
  confirm it is byte-identical (nothing upstream touched it) as a sanity
  check that the memory core truly stayed untouched.

## Sequencing after all 7 batches land

Once all batches are merged and green, the resulting branch should be
proposed as the new `nightly` tip (fast-forward or merge `nightly` into
it), and the stale-`nightly`-vs-`main` divergence (20 commits) reconciled
in the same PR, restoring the documented `nightly → main` flow. That
reconciliation is out of scope for *this* session (batch 1 only).
