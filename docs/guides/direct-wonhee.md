# Direct Wonhee tools

Owner-approved 2026-10-25: main Moeka is the only conversational agent. It uses
Wonhee MCP for creative memory/projects/media and Stagecraft production. The bridge
keeps intake and delivery receipts, enriches the original Telegram turn with a
host-authenticated creative context, and never routes it to another agent in direct
mode. Existing peer mode is compatibility only.

Creative project selection preserves the initiating brief in the current Telegram
conversation. Host-minted metadata binds source/request/route for MCP actions and
cannot be forged by model arguments or arbitrary client metadata. No new permissions
to protected media directories; bounded reference previews come through Wonhee.

Expected non-retryable tool failures are structured MCP errors. Two identical
permanent failures end a turn; the direct gateway also caps schema/tool errors.
Legacy Stagecraft dictionary submission stays off the main agent's allowlist;
the typed stagecraft_build tool takes ordinary brief/title/capture/reference fields.

Deployment preserves general-agent personality/history and existing creative data,
stops the redundant Wonny service, and installs the tools on main Moeka. No second
Telegram poller, automatic uncertain-send replay or global sandbox relaxation.

Implementation contract: sibling wonhee/docs/specs/2026-10-25-direct-moeka-design.md.

Deployed and independently reviewed 2026-10-25. The live gateway is healthy, with
27 Wonhee MCP tools and wonhee_project registered on main Moeka. Wonny is disabled,
with its saved files/history preserved. The creator's original brief/reference
remain selected. Real configured-model acceptance queued a referenced build, then
Codex + offline Docker produced a verified stdio dashboard video. Delivery used a
local captured sink and was deduplicated; no Telegram API test message was sent.
See sibling stagecraft/docs/DIRECT_MOEKA.md for artifact evidence.

Validation: 105 focused bridge/MCP/runner tests pass. Ruff passes for changed code.
Basedpyright reports zero issues for creative_context.py, creative_bridge.py and
wonhee_handoff.py. Broader changed-module checks retain existing MCP optional-env/
OAuth and schema forward-reference diagnostics outside the changed lines; recorded
privately in /mnt/arteta/stagecraft/deployments/direct-pyright.txt.

## Delivery details (spec 014)

- A finished file is sent as one message: the video with its report as the caption (reports over
  1024 characters go as a following text message).
- A video over Telegram's 50 MB bot limit is re-encoded under it for the inline copy; the
  original stays at the download URL. If it cannot be fitted and a URL exists, the message carries
  the link and the reason; without a URL the delivery is marked failed with the reason.
- Each user turn's `[Wonhee project context]` includes `recent_deliveries` (last five: file,
  status, size, report), so a status question does not need polling.
- The agent cannot watch a video directly: it uses `view_media` (frames, loudness, a cited answer
  from the `viewer` preset) and must say what it could not verify.

## The link contract (spec 014)

- Wonhee publishes a versioned contract through its host-only `contract` action: contract version, source
  revision (`git describe`), every action name, the response keys the bridge reads, and the signal marker.
  It is not an agent tool.
- Moeka checks it at gateway start and every 10 minutes (wonhee runs from a live tree, so it can change
  under a running gateway). A mismatch is logged as an error and shown at the top of `/wonhee status`
  (for example `wonhee has no 'outbox_list' action`); the signal marker is not trusted from a mismatching
  wonhee, so the bridge falls back to polling.
- The keys moeka reads are `REQUIRED_SHAPES` in `nanobot/creative_bridge.py`; wonhee's side is
  `BRIDGE_SHAPES` in `wonhee/creative.py`. Wonhee's own test checks every entry against the real responses,
  and `tests/test_wonhee_contract_live.py` runs the real wonhee CLI against a temporary home. Changing a
  response key on either side without the other fails a test.
- Compatible changes (new keys, new actions) need no version bump. Removing or renaming an action or a
  promised key bumps `CONTRACT_VERSION`, and moeka lists the versions it supports in
  `SUPPORTED_CONTRACT_VERSIONS`.

## Polling without spawning a process

- The bridge used to start a Python process every 2 seconds to ask for deliveries. It now `stat`s a marker
  file (`<WONHEE_HOME>/.bridge-signal`) that wonhee touches only when a delivery is queued, failed,
  retried or marked uncertain, or a job fails. Heartbeats and ordinary progress do not touch it (wonhee's
  database itself changes about every 2 seconds, so it cannot serve as the signal).
- A safety poll still runs every 30 seconds, so a missed marker costs at most that delay. A wonhee process
  that has not been restarted onto the new code does not touch the marker; deliveries and failures then
  surface within 30 seconds instead of at once. Restart `wonhee-creative` and `stagecraft-worker` after
  upgrading wonhee.
- After a failed check the bridge backs off (2s doubling to 60s) and logs the first failure at warning and
  the third at error with the redacted reason; it logs again when the link recovers.

## Who owns what

- Wonhee owns creative projects, assets, memory, briefs, submissions and every delivery's state
  (`creative_delivery`). Moeka never writes these tables; it moves a delivery through
  `queued, sending, sent|failed|uncertain` only via `delivery_show` transitions.
- Moeka owns the Telegram transport state in `peers/creative.sqlite3`: receipts, text responses
  (`outbound`), destinations and the `delivered` message ids.
- Wonhee's `creative serve` worker only runs jobs and serves download links; it does not send to Telegram, so
  there is one sender. A worker restart marks in-flight sends `uncertain`; a real send acknowledgement from
  the bridge (with Telegram message ids) overrides that, so a delivered file is never stuck.
- `/wonhee status` shows both sides: moeka's receipts and responses, the contract, wonhee's stuck deliveries
  (with age and reason) and active jobs (with `STALLED`). `/wonhee retry` re-queues the newest failed or
  uncertain text response and the newest failed or uncertain wonhee delivery for the chat.
- A job that stops moving (no heartbeat for 10 minutes while running, or unclaimed for 30 minutes) is
  announced to the creator once; a failed reel's notice now carries the reason.

## The self-check gate (spec 014)

- Before a wonhee delivery is sent, the bridge hashes the file, looks for an earlier delivery in the same
  project with identical bytes (wonhee stores outputs by content hash), and measures what ffprobe/ffmpeg can:
  resolution, duration, fps and audio loudness. The result is stored in `delivery_check` in
  `peers/creative.sqlite3`.
- The report that travels as the caption is wonhee's text plus: a `WARNING: byte-identical to delivery N`
  line when the output did not change, `Measured: ...`, the short file hash, and `Not checked: caption
  legibility, timing and motion`. It replaces wonhee's `Full file check: not yet run.`
- The agent's project context lists each recent delivery with `sha`, `duplicate_of` and `measured`
  (`checked: false` for one sent before the gate), plus `evidence_rules`: say a change worked only for a new
  file that was also viewed with `view_media`; if `duplicate_of` is set, tell the creator nothing changed.
- `view_media` reports the viewed file's `sha256` prefix, so a claim can name the exact file it checked and be
  compared with the delivered `sha`.
- A failing check never blocks the delivery; it is sent without the extra lines and a warning is logged.
- Limits, stated: the gate measures and compares bytes; it does not judge how a reel looks. It also does not
  stop the agent from submitting an identical job again; the agent learns of the duplicate when it is
  delivered, and a pre-submission block would need wonhee to expose a render fingerprint.

