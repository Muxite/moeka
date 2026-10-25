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
