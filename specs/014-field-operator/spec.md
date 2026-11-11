# 014 Field operator

Make moeka a dependable Telegram-fronted operator: relay video, image and text to wonhee, run and
watch work on the machine, and report honestly what was and was not checked. Plan source:
`~/.claude/plans/make-a-new-major-polymorphic-lampson.md` (2026-11-02 interview).

## Decisions

| # | Decided | Chosen | Ruled out, and why |
|---|---------|--------|--------------------|
| 1 | priority | wonhee relay loop first | general operator first: leaves the observed pain longest |
| 2 | capability | stronger moeka, self-check gate, escalation | escalation only: Claude Code on every hard step is the overkill to avoid |
| 3 | RSI | trace emission only | opt-in mutation hook: more surface, not needed |
| 4 | main model | Luna, local GPU fallback (`modelRouting`) | local as main: not asked for |
| 5 | viewing media | `viewer` preset (`google/gemini-3.5-flash-lite`) | wonhee `media_info` only: led to unverified claims |
| 6 | file reads | broad read-only `tools.file.readRoots`, floor stays | workspace-only: caused the Stagecraft dead end |

## Correction to the first findings

- The transcript of `telegram:8389948559` suggested finished videos never reached Telegram.
  That was wrong: bridge deliveries are sent outside the agent loop and are absent from the
  session. `wonhee.db` `creative_delivery` shows all 12 deliveries `sent` (2026-10-24 to 10-26).
- The real gaps are the thin delivery text ("Full file check: not yet run."), the agent not
  knowing a delivery happened, and claims of a fix made without a changed output or a view.

## Status

- Phase 0 baseline: Docker subset green (3084 passed). Full local suite green (8964 passed).
- Phase 1a `tools.file.readRoots`: done (`filesystem.py`, `tests/tools/test_file_read_roots.py`).
- Phase 1b `view_media`: done (`agent/tools/view_media.py`, `utils/media_probe.py`,
  `tools.viewMedia`); Telegram video placeholder tells the model it cannot see the clip.
- Phase 2 delivery: done in part.
  - `channels/telegram/media_send.py`: photo over 10 MB as document, bounded re-encode of a video
    over 50 MB, clear `MediaTooLargeError` reason, width/height/duration for videos.
  - `message` tool: `caption`, `as_document`.
  - Bridge `_send`: oversize video fitted (original stays at the URL), report sent as the file
    caption (one message), URL fallback; direct-mode context gains `recent_deliveries`.
  - Not built, deliberately: a generic job watcher (wonhee already queues and the bridge already
    sends deliveries); `caption`/`caption_lines` and `media_info` auto-register fixes (wonhee-side,
    weak evidence); skip-unchanged context blob (risk when history compacts).
- Phase 7 wonhee link (2026-11-03), both repos:
  - `contract` action + startup/periodic check, `REQUIRED_SHAPES` vs `BRIDGE_SHAPES`, live cross-repo test.
  - Poll by marker file instead of a process every 2 s (wonhee.db itself changes every ~2 s, measured).
  - Specific failure reasons, stalled-job announcements, richer `/wonhee status` and `/wonhee retry`,
    `active_jobs` in the agent's project context, backoff with real error logging.
  - Wonhee: an acknowledged send overrides a recovery-marked `uncertain` delivery.
  - Audit claims that did not hold: missing `enabledTools` entries already warn loudly; the wonhee child's
    stderr already reaches the gateway journal; `creative serve` does not send to Telegram.
- Phase 3 self-check gate (2026-11-03): built at delivery time, where it can be enforced in code.
  - `nanobot/creative_checks.py`: duplicate detection by content hash, measured facts, the honest report,
    `EVIDENCE_RULES`; `delivery_check` table; `recent_deliveries` carries `sha`/`duplicate_of`/`measured`.
  - `view_media` reports a `sha256` prefix. End-to-end test of the Oct 26 identical-reel failure runs
    against the real wonhee CLI.
  - Not built, deliberately: free-text claim detection in the runner (fragile, would block valid replies);
    a pre-submission duplicate block (needs a render fingerprint from wonhee); escalation (Phase 4).
- Phases 4 to 6: not started (escalation, staying on track, traces).

## Host-ambient allowlist

`tests/kernel/test_no_ambient_reads.py` gained reviewed entries for `agent/tools/tmux.py`,
`creative_bridge.py` and `creative_context.py` (host-owned, written reasons).
