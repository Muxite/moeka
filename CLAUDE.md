# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repo is **moeka**, a fork of [nanobot](https://github.com/HKUDS/nanobot) (HKUDS). `main` is the consolidated
distribution (003, owner decision 2026-09-30): the **agent kernel** is the base (`moeka` package, `nanobot/kernel`,
the agent loop, providers, tools, skills, prompt templates, memory/Dream, SQLite sessions, `nanobot/llm_usage`,
specs and `.agent/` design docs), and the **gateway host** sits on top of it as a consumer: chat channels, gateway,
WebUI, HTTP API server, pairing, audio, cron, triggers, heartbeat, CLI apps and the `message` tool. The live
systemd service runs the gateway from this branch. The kernel is also driven by the RSI harness and awork (pinned
submodule), by `moeka.Kernel`, or by the `nanobot agent` CLI.

Dependency direction: host packages import the kernel, never the reverse (enforced by
`tests/core/test_import_boundary.py`; ambient process reads are confined by `tests/kernel/test_no_ambient_reads.py`,
which carries a reviewed `HOST_AMBIENT_ALLOWLIST` for the gateway packages). Host-owned tools (`message`, `cron`,
`run_cli_app`) load only for loops built with `host_tools=True` (gateway, `serve`, CLI agent); a bare kernel loop or
`moeka.Kernel` agent never gets them.

Moeka-specific deviations from upstream nanobot:

- **Permissive shell sandbox** — `nanobot/agent/tools/shell.py` always applies a non-removable floor,
  `_FLOOR_DENY_PATTERNS` = `_INTERNAL_DENY_PATTERNS` (writes to `history.jsonl` / `.dream_cursor`) plus the fork
  bomb. The floor is checked before the allow-pattern exemption, so `allowPatterns` cannot exempt it, and
  `tools.exec.denyPatterns` (config or constructor, `[]`/None included) can only ADD to it. `rm -rf`, `dd`,
  `mkfs`, `shutdown` are not blocked. `exec_session` stdin is screened with the same floor, deny, sudo and
  internal-URL checks (`ExecTool.check_session_input`, passed as the session's `input_guard`; no allowlist or
  path checks). That is a hint for line-oriented shells only: REPLs can still receive anything, and real
  containment is the sandbox/host.
  `allow_sudo` defaults to False and the denial message explains the opt-in.
- **SQLite session store outside the workspace** — `nanobot/session/sqlite_store.py`: one `sessions.db` (WAL) at
  `<workspace parent>/<workspace name>-sessions/<workspace-id>/sessions.db` (e.g. `~/.nanobot-sessions/<id>/`),
  never inside the workspace. A legacy `<workspace>/sessions.db` is only warned about; moving it is
  `nanobot sessions migrate`. Legacy `.jsonl` sessions are imported once and renamed `*.jsonl.imported`;
  `SqliteSessionStore.dump_jsonl(key)` exports the old format.
- **`bg_shell` is gated off the auto-loader** — `BackgroundShellTool.enabled()` returns False; nothing wires a
  `BackgroundProcessRegistry`, so it is dormant.
- **Lazy `Config` / `ToolsConfig` model_rebuild** — `nanobot/config/schema.py` (`_resolve_tool_config_refs`,
  `Config.__init__`) and `load_config` retry forward-ref resolution to survive circular-import order.
- **Flat workspace layout** — `AgentDefaults.workspace` defaults to `~/.nanobot` (the state home is the workspace),
  not upstream's nested `~/.nanobot/workspace`.
- **Missing `${VAR}` config references warn, not hard-fail** — `resolve_config_env_vars`
  (`nanobot/config/loader.py`) logs a warning with the dotted field path and leaves the placeholder. See
  `tests/config/test_env_var_warnings.py`.
- **Gateway sections are live config** — `channels`, `gateway`, `api`, `heartbeat`, `transcription` belong to the
  host and load normally (the slim-era "retired sections" stripping was removed in the consolidation). Kernel-only
  hosts simply never set them.
- **Dream** — callable directly (`nanobot/agent/dream.py:run_dream()`, `AgentLoop.run_dream()`); the gateway also
  schedules it as a cron system job (`DreamConfig.build_schedule`, `nanobot/cli/gateway_runtime.py`).
- **Dispatcher watchdog** — `ChannelManager._dispatch_with_watchdog` auto-restarts the outbound dispatcher on crashes.
- **`nanobot channels enable/disable <name>` CLI** — atomic config flip, defined in `nanobot/cli/commands.py`.
- **Telegram `drop_pending_updates` defaults to True** to avoid stale floods on restart.
- **Transcription `api_base` propagation** — Groq/OpenAI Whisper provider honours per-provider `api_base`.
- **No CONTRIBUTING.md and no upstream `images/nanobot_logo.png`** — both intentionally removed; moeka uses its own `images/GitHub_README.png`.
- **Gateway still on the legacy loop path** — the gateway builds `AgentLoop` through `AgentLoop.from_config` with a
  `LegacyEnvironment`, not through `moeka.Kernel`; moving it is stage 2 of `specs/003-main-consolidation/`.

## Documentation for agents

`docs/core-map/README.md` is the authoritative, line-cited map of the kernel (agent loop, tools, prompts/skills/
memory, config/providers/sessions) and states what a self-improvement agent may and may not change. The rest of
`docs/` documents the gateway host and the embedding API (`python-sdk.md`, `migration-moeka-api.md`).

## Development Commands

```bash
# Tests in Docker (preferred: isolated from the live service and the host venv).
# Dockerfile.test keys its dependency layer on pyproject.toml + uv.lock only, so
# code edits rebuild quickly. The vec extra (CUDA torch) is skipped by default;
# --build-arg NO_EXTRA= re-enables it.
scripts/test-docker.sh                              # build + full suite
scripts/test-docker.sh pytest tests/agent -v        # build + subset
scripts/test-docker.sh ruff check nanobot/ tests/   # lint (never ruff format)

# CLI (entry point nanobot/cli/entry.py -> nanobot/cli/commands.py)
nanobot agent [-m "message"]    # interactive chat, or one message
nanobot status | sessions | provider
nanobot gateway                 # the host: channels + WebUI + cron + HTTP API

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev && bun run build && bun run test
```

## Architecture

- **Agent loop** (`nanobot/agent/loop.py`): `AgentLoop` owns sessions and runs each turn through fixed stages
  (restore, compact, command, build, run, save, respond). Entry points: `process_direct()` and the `MessageBus`
  (`nanobot/bus/`) consumed by `run()`.
- **Runner** (`nanobot/agent/runner.py`): the LLM/tool iteration loop, error shaping and limits.
- **Context** (`nanobot/agent/context.py`, `nanobot/templates/`): system-prompt assembly from Jinja templates,
  workspace bootstrap files, memory and the skills summary.
- **Providers** (`nanobot/providers/`): Anthropic, OpenAI-compatible (incl. local vLLM/Ollama), OpenAI Responses,
  Azure, Bedrock, OAuth providers; `factory.py` / `registry.py`; `FallbackProvider`; model presets.
- **Tools** (`nanobot/agent/tools/`): auto-discovered by `ToolLoader` (pkgutil + entry points); the registered set
  is pinned by `tests/agent/test_registered_tool_names.py`. MCP servers via `mcp.py`.
- **Skills** (`nanobot/skills/`, `<workspace>/skills/`) and **memory/Dream** (`nanobot/agent/memory.py`,
  `nanobot/agent/dream.py`).
- **Sessions** (`nanobot/session/`), **config** (`nanobot/config/schema.py`, `loader.py`), **commands**
  (`nanobot/command/builtin.py`), **security** guards (`nanobot/security/`).
- **Gateway host** (the live bot): `nanobot/channels/` (platform runtimes, `manager.py`), `nanobot/gateway/`,
  `nanobot/webui/` + `webui/` (React SPA), `nanobot/api/server.py` (OpenAI-compatible HTTP API),
  `nanobot/cron/`, `nanobot/triggers/`, `nanobot/apps/` (CLI apps), `nanobot/pairing/`, `nanobot/audio/`,
  `nanobot/cli/gateway*.py`. Consumes the kernel through `AgentLoop` and the `MessageBus`.
- **Embedding**: `MoekaKernel` = `MoekaCore` (`nanobot/core/`, re-exported by `nanobot/kernel/`; import boundary enforced by `tests/core/test_import_boundary.py`)
  and the `Nanobot` SDK facade (`nanobot/nanobot.py`).

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Branching Strategy

- `main` — the consolidated distribution (kernel base + gateway host); the running systemd unit follows it and
  upstream syncs land here.
- `core-slim` — historical, pinned: the RSI harness and awork pinned `core-slim` commits (`6f80c392`). Never
  rebased, force-pushed or deleted; it stops receiving merges once consumers move to `main`.
- `nightly` — retired 2026-09-28 (alias of the old `main`); do not integrate there.

The `upstream` remote points at `HKUDS/nanobot`. Flow: `upstream/main` → merge branch off `main` (for example
`merge/upstream-main-<date>`) → `main` after tests and a deploy check. Upstream edits to kernel-owned paths
(`nanobot/agent`, `providers`, `tools`) need a check against the kernel contract (`specs/`, `.agent/`). Moeka
deviations must be preserved during conflict resolution. The cutover record is
`specs/003-main-consolidation/CUTOVER.md`.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored). Never run `ruff format`.
- pytest with `asyncio_mode = "auto"`. Tests mirror the `nanobot/` package structure.
- Diagrams: mermaid only (`flowchart`, `sequenceDiagram`, `stateDiagram-v2`); no ASCII art. Design docs use
  firm bullet points, with tables only for genuinely tabular data.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Tool base / registry / loader: `nanobot/agent/tools/base.py`, `registry.py`, `loader.py`
- Prompt templates: `nanobot/templates/agent/`
