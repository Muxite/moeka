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
