# Plugin-seam assessment — can moeka's divergences ride upstream's own seams?

Read-only analysis, requested after batch 2, in response to: "moeka kept
nanobot's original approach mainly to keep merges cheap — is that premise
still true?" No code changed for this. Based on upstream checkpoints
`67805f5d` (2026-08-05) and `0c684c5a` (2026-08-12), the two batches
merged so far.

## 1. `SessionStore` Protocol / `JsonlSessionStore`

Upstream's shape: a `SessionStore` Protocol (`load`, `save`, `delete`,
`read`, `read_metadata`, `list_sessions` — 6 methods) plus a thin
`SessionManager` wrapper that owns the **backend-agnostic** parts: the
LRU cache (`_cache`/`_overflow_cache`/`get_cached`), `Session`/
`SessionPolicy`/`RetentionResult` dataclasses, `get_or_create_transient`,
`fork_session_before_user_index`, and (as of 08-12) the compaction-
boundary continuity fix in `Session.get_history()`. `JsonlSessionStore`
only implements the 6 Protocol methods plus its own file-format concerns
(ADR-0001 workspace-outside placement, per-file migration/checksums).

**If moeka wrote `SqliteSessionStore(SessionStore)`** instead of
maintaining a forked `SessionManager`: the ~700 lines of actual SQL logic
(schema, `_conn()`, `save`/`_load`/`list_sessions`/`read_session_metadata`)
barely change — they're mostly already organized as private methods on
what would become the Store class; it's a rename/reshape, not a rewrite.
`nanobot/session/manager.py` itself would then merge with the wrapper
**unchanged**, because moeka's edits would live in a new
`nanobot/session/sqlite_store.py`-shaped file upstream never touches.
Every wrapper-level feature this batch's conflicts were actually about
(`SessionPolicy`, `get_or_create_transient`, the delivery-retention fix,
`MIN_COMPACTED_REPLAY_MESSAGES`, `get_cached`) would arrive for free on
every future merge instead of needing manual re-porting — that category
was the majority of the batch-1/2 session work.

**What moeka would lose:** (a) unilateral control over wrapper-level
policy decisions (upstream's shape becomes the shape, no local override
without patching); (b) two direct SQLite-internals leaks would need a
seam — `nanobot/webui/session_list_index.py` and
`nanobot/agent/memory.py` both call `session_manager._conn()` directly
for bounded raw-SQL scans (preview text, dream-session pruning). These
aren't in the Protocol and would need to become `SqliteSessionStore`
methods reached via `session_manager._store._conn()` or a small
moeka-only accessor added to the Store subclass (non-conflicting, since
upstream's wrapper doesn't define that name). `restore_sessions_to_workspace`
(new CLI command target) is the same story — an addable Store method, not
a wrapper change. WAL-checkpoint `close()` on shutdown is currently a
`SessionManager` method; it'd move to the Store, one call-site update in
`gateway_runtime.py`.

**Risk:** the Protocol itself is still moving (1174→1699 lines batch-to-
batch) — a couple more batches could still reshape method signatures,
meaning this migration isn't "do once and never touch again" *yet*, but
each adjustment would be Protocol-sized, not full-file-sized.

**Estimate:** 1–2 days one-time migration; recovers the majority of what
made `session/manager.py` the largest conflict in both batches so far.

## 2. Agent Plugins v1 (`nanobot/agent/plugins.py`)

Concretely offers: (1) skill discovery from `<workspace>/plugins/*/skills/`
(`enabled_agent_plugin_skills()` — already integrated in batch 2);
(2) declarative MCP server registration via a package's `mcp.json`, with
contained `PLUGIN_ROOT`/`PLUGIN_DATA` paths for stdio servers;
(3) an `extensions.*` manifest namespace (currently just a logo field) —
the closest thing to a general extension point, but nothing is wired to
it yet beyond that one field. It is a **packaging/discovery** mechanism
for skills + MCP servers, explicitly "not a replacement for native
providers, channels, tools, ... or directly configured MCP servers" (its
own docs). No code-level hook for tool registration, sandbox policy,
channel supervision, or CLI subcommands.

Checked against moeka's deviations: none of the expensive ones (session
store, dispatcher watchdog) are expressible through it — it has no seam
for either. The config-default deviations (shell sandbox, Telegram
default) were never really "plugin-shaped" problems; they're schema
defaults already. bg_shell's `enabled(ctx)` override already uses an
*existing* tool-level seam (every `Tool` subclass has this hook) — Agent
Plugins v1 doesn't change that story either way.

## 3. Classification of every `CLAUDE.md` deviation

| Deviation | Class | Why |
|---|---|---|
| Permissive shell sandbox / `allow_sudo=False` | **(a)** | Already just `ExecToolConfig` field defaults in schema.py; one small hunk, never conflicted across 2 batches. |
| SQLite session store (in-workspace default) | **(c)→(b)** | Forked today; becomes (b) via `SqliteSessionStore` against §1. Highest-value target. |
| Dispatcher watchdog (`_dispatch_with_watchdog`) | **(c)** | No upstream seam for channel-transport supervision exists. Genuinely forked, but small/stable — unchanged across both batches. |
| `bg_shell` gated off auto-loader | **(a)** | Already expressed through the existing `Tool.enabled(ctx)` hook every tool has. Not really a fork. |
| `channels enable/disable` CLI | **(c)** | New Typer subcommand; Agent Plugins v1 has no CLI surface. Low cost in practice (clean both batches). |
| Telegram `drop_pending_updates=True` | **(a)** | Channel config field default. |
| Transcription `api_base` propagation | **(a)/(b)** | Small provider-code diff; plausibly a genuine upstream bug (providers should honor per-provider `api_base`) — worth a real PR, would remove the diff entirely. |
| Lazy `Config`/`ToolsConfig` model_rebuild | **(c)→(b)** | Defensive circular-import fix; not moeka-specific in spirit, worth upstreaming as a general robustness fix. |
| No `CONTRIBUTING.md`/logo, `GitHub_README.png` | **(a)** | Branding/docs, zero technical merge cost either way (delete-kept or `checkout --ours`). |
| `.agent/*`, `CLAUDE.md`, `bin/*` launchers | **(a)** | moeka-only paths, never touched upstream, zero conflict risk. |
| `tools.exec.allowPatterns` whitelist semantics | **(a)** | Already upstream's own scoping mechanism (`tools/loader.py`); moeka's contribution here is the allow/deny param surface on `ToolLoader.load`, which is small and has merged clean twice. |

## Honest cost estimate

`session/manager.py` accounted for the large majority of actual
conflict-resolution *time* in both batches (not file count — 9-21 files
conflicted total, but this one file's resolution, verification, and
hunting for silently-missing fixes dominated). Adopting the Protocol
would remove that specific recurring cost almost entirely, converging to
occasional small Protocol-shape adjustments instead of full-file
re-review. The rest of the deviation list is either already cheap (a) or
small-and-stable forks (c) that haven't caused rework across 2 batches.
**Rough estimate: converging session storage to `SqliteSessionStore`
removes roughly half of the recurring per-batch merge cost seen so far;**
the remainder (dispatcher watchdog, CLI command, occasional shell.py
churn) is comparatively cheap and not obviously worth chasing further.
