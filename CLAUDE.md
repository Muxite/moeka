# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repo is **moeka**, a fork of [nanobot](https://github.com/HKUDS/nanobot) (HKUDS). This branch,
`core-slim`, is the **slim agent core**: the agent loop, LLM providers, tools, skills, prompt templates, memory
(Dream), the SQLite session store, config, and the embeddable `MoekaCore` library. The chat channels, gateway,
WebUI, HTTP API server, pairing, audio, cron, triggers, heartbeat, CLI apps and the `message` tool are removed
here; they live on in the full chat-bot distribution on `main`. The core is driven by a harness (a separate repo
that pins a `core-slim` commit as a submodule), by `MoekaCore`, or by the `nanobot agent` CLI.

Moeka-specific deviations from upstream nanobot that still exist in the core:

- **Permissive shell sandbox** — `nanobot/agent/tools/shell.py` always applies a non-removable floor,
  `_FLOOR_DENY_PATTERNS` = `_INTERNAL_DENY_PATTERNS` (writes to `history.jsonl` / `.dream_cursor`) plus the fork
  bomb. The floor is checked before the allow-pattern exemption, so `allowPatterns` cannot exempt it, and
  `tools.exec.denyPatterns` (config or constructor, `[]`/None included) can only ADD to it. `rm -rf`, `dd`,
  `mkfs`, `shutdown` are not blocked. Known gap: `exec_session` calls `ExecTool._spawn` directly and never
  `_guard_command`, so a fork bomb can still be fed to a running shell through `exec_session` stdin.
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
- **Retired config sections are dropped** — `channels`, `gateway`, `api`, `heartbeat`, `transcription` are removed
  at load with a warning (`_migrate_config`), and saving the config removes them from the file. Do not point slim
  tooling at a `config.json` shared with a `main` deployment.
- **Dream runs only when called** — no scheduler: `nanobot/agent/dream.py:run_dream()`, `AgentLoop.run_dream()` or
  the `/dream` command. `DreamConfig.enabled` / `interval_h` are advice for the caller.

## Documentation for agents

`docs/core-map/README.md` is the authoritative, line-cited map of this core (agent loop, tools, prompts/skills/
memory, config/providers/sessions) and states what a self-improvement agent may and may not change. On this
branch the rest of `docs/` describes the full distribution on `main` and is legacy (see the banner in
`docs/README.md`).

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
- **Embedding**: `MoekaCore` (`nanobot/core/`; import boundary enforced by `tests/core/test_import_boundary.py`)
  and the `Nanobot` SDK facade (`nanobot/nanobot.py`).

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Branching Strategy

- `core-slim` (this branch) — the slim core. The harness repo pins a `core-slim` commit as its core submodule.
  Do not merge it into `main`.
- `main` — the full chat-bot distribution; the live systemd service follows it.
- `nightly` — integrates upstream `HKUDS/nanobot` plus moeka work for `main`. The `upstream` remote points at
  `HKUDS/nanobot`; moeka syncs from `upstream/main` (upstream's `nightly` went stale after 2026-06-03).
  Moeka deviations must survive conflict resolution.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored). Never run `ruff format`.
- pytest with `asyncio_mode = "auto"`. Tests mirror the `nanobot/` package structure.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Tool base / registry / loader: `nanobot/agent/tools/base.py`, `registry.py`, `loader.py`
- Prompt templates: `nanobot/templates/agent/`
