# Upstream sync working notes — 2026-09-16

## Branch
`merge/upstream-main-2026-08-05`, created off `main` @ `2b19da40` (never
checked out/committed to `main` itself). Batch 1 target checkpoint:
`upstream/main` @ `67805f5db8dc8d3b1a61fbc0f29bb1a6d010b86e` (2026-08-05,
134 commits ahead of `main`).

## Survey findings (see plan for full detail)
- `merge/upstream-main-2026-07-28` and `-08-03` branches are stale/closed:
  fully merged into `main` already (0 unique commits vs `main`, both still
  744 behind `upstream/main` — same as `main`). Not "in-flight".
- `nightly` is stale (2026-05-17), 20 commits behind `main`, 1951 behind
  `upstream/main`, 43 ahead of `upstream/main` — bypassed by direct
  commits to `main`. Flagged for owner, not fixed here.
- `feat/sqlite-vec-memory` fully merged into `main`, irrelevant.
- `main..upstream/main`: 744 commits, 1165 files, +197725/-51116,
  2026-07-28→2026-09-16.
- `nanobot/core` (moeka's plugin/RAG core, MoekaCore, VecStore): 0
  upstream commits touch it in this window.
- `session/manager.py`: 27 upstream commits. `tools/shell.py`: 14 upstream
  commits including a deny-pattern-scoping branch — highest scrutiny.
  `tools/registry.py`: 5 upstream commits.

## Batch 1 log (in progress)
(filled in as merge proceeds)
