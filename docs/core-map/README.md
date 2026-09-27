# Core map

The authoritative description of the moeka slim kernel (`core-slim` branch), written for a self-improvement
agent that edits skills, prompts, tool descriptions and config, and for the harness that supervises it. Every
behavioural claim cites `path:line` in the current tree; citations are relative to `nanobot/` unless they start
with `tests/`, `docs/` or `.agent/`. Anything not confirmed by reading code is marked **unverified**.

| Doc | Covers |
|---|---|
| [01-agent-loop.md](01-agent-loop.md) | Turn stages, `AgentLoop` / `AgentRunner`, system-prompt assembly order, limits and knobs, failure modes, goals and continuation |
| [02-tools.md](02-tools.md) | Tool discovery/registration/scoping, execution and error shaping, the per-tool table (where each description and schema lives), description-writing conventions, tool-call validation |
| [03-prompts-skills-memory.md](03-prompts-skills-memory.md) | Template inventory, SKILL.md format and validation, skill discovery, memory files, Dream (`run_dream`), consolidation, test pins on prompt text |
| [04-config-providers-core-sessions.md](04-config-providers-core-sessions.md) | Config loading and knobs, provider selection/retry/fallback, a local vLLM preset example, `MoekaCore`, the SQLite session store and crash behaviour |
| [05-kernel.md](05-kernel.md) | The `nanobot/kernel/` modules mapped to invariants I1-I6: host env, gate and floors, router and ledger, plugins and typed calls, fact/artifact stores and clarification (`kernel.facts`, `.artifacts`, `.propose`, `.answer`) |

## Architecture in six lines

1. A caller either awaits `AgentLoop.process_direct()` (used by `MoekaCore.run`, `nanobot agent -m`, Dream) or publishes an `InboundMessage` on the `MessageBus` that `AgentLoop.run()` consumes (interactive `nanobot agent`).
2. Each turn runs fixed stages: restore, compact, command (slash commands), build, run, save, respond (agent/loop.py:1747-1754).
3. `ContextBuilder` assembles the system prompt from templates, workspace bootstrap files, memory and the skills summary (agent/context.py:133-206); tools go separately as provider `tools` definitions.
4. `AgentRunner` loops provider call -> tool calls -> tool results until a final answer or `maxToolIterations` (agent/runner.py:478).
5. Sessions persist in SQLite outside the workspace; memory is `memory/history.jsonl` + `MEMORY.md`/`SOUL.md`/`USER.md`, consolidated by Dream only when a caller runs it (agent/dream.py:41).
6. Config is one pydantic `Config` (`agents`, `display`, `providers`, `tools`, `modelPresets`, `profiles`); retired chat-runtime sections are dropped at load with a warning.

## What a self-improvement agent may change

- **Skills.** Workspace skills `<workspace>/skills/<name>/SKILL.md` (plus `scripts/`, `references/`, `assets/`)
  and built-in skills `nanobot/skills/<name>/`. Format and precedence: doc 03 section 2. Workspace skills are not
  git-tracked; keep your own backup.
- **Prompt templates.** `nanobot/templates/agent/*.md` and `nanobot/templates/agent/_snippets/*.md` (doc 03
  section 1a; keep every Jinja variable the caller passes), the workspace bootstrap templates
  `nanobot/templates/AGENTS.md`, `SOUL.md`, `USER.md`, `memory/MEMORY.md` (doc 03 section 1b; they only reach new
  workspaces), and the workspace override `<workspace>/prompts/dream.md` (doc 03 section 1d). Never edit
  `nanobot/templates/legacy/SOUL.md` (detection fixture).
- **Tool descriptions and parameter schemas.** Only the description text and per-property `description=` strings,
  at the locations in doc 02 section 2 (column "Description" for the tool description, column "Schema" for the
  parameter block). Summary of where each lives:
  read_file / write_file / edit_file / list_dir in `agent/tools/filesystem.py`; apply_patch in
  `agent/tools/apply_patch.py`; find_files / grep in `agent/tools/search.py` (raw-dict `parameters` properties);
  exec in `agent/tools/shell.py` (`description` property + `@tool_parameters` block); exec_session /
  list_exec_sessions in `agent/tools/exec_session.py`; web_search / web_fetch in `agent/tools/web.py` (class
  attribute `description`); spawn in `agent/tools/spawn.py`; ask_user in `agent/tools/ask.py`; my in
  `agent/tools/self.py`; create_goal / update_goal in `agent/tools/long_task.py`; generate_image in
  `agent/tools/image_generation.py`; search_sessions / read_session in `agent/tools/sessions.py`; list_sessions /
  send_session_message in `agent/tools/session_messages.py`. Never change a tool `name`, parameter name, type,
  enum value, `required` list, or an error string containing a marker phrase (doc 02 section 1, step 6).
- **Config presets and sampling parameters.** Entries under `modelPresets.<name>` (`model`, `provider`,
  `maxTokens`, `contextWindowTokens` within the server limit, `temperature`, `reasoningEffort`),
  `agents.defaults.modelPreset`, `agents.defaults.fallbackModels` (names must exist in `modelPresets`),
  `agents.defaults.dream.modelOverride`, `planning`, `disabledSkills` / `allowedSkills`,
  `limits.toolFailureReflectionThreshold`, `idleCompactAfterMinutes`, `vec.*K`. Doc 04 sections 2 and 6 list
  defaults, effects and the safe/careful/dangerous split; stay in "safe" unless a human approves.

## What it must not change

- The evaluator / harness that scores and gates its changes, and anything outside this kernel repo.
- Python code in general, and specifically the session store (`nanobot/session/`), the agent loop and runner, and
  the security guards: `nanobot/security/`, exec deny/allow patterns and sudo gating in
  `nanobot/agent/tools/shell.py`, the SSRF/workspace marker phrases in `nanobot/agent/runner.py`, the `my` tool's
  BLOCKED/READ_ONLY/RESTRICTED lists in `nanobot/agent/tools/self.py`.
- Tests (`tests/**`). If a test pins wording (tests/tools/test_tool_descriptions.py,
  tests/agent/test_context_builder.py, tests/agent/test_consolidator.py, tests/agent/test_skills_loader.py,
  tests/agent/test_prompts_no_removed_features.py), keep the pinned phrase instead of editing the test.
- Secrets and security-relevant config: `providers.*` (`apiKey`, `apiBase`, `proxy`, `extraHeaders`, `extraBody`),
  `${VAR}` references, `tools.exec.*` (`allowPatterns`, `denyPatterns`, `allowSudo`, `sandbox*`),
  `tools.restrictToWorkspace`, `tools.ssrfWhitelist`, `tools.mcpServers`, `agents.defaults.workspace`, `profiles`.
- Memory files `MEMORY.md`, `SOUL.md`, `USER.md` and `memory/history.jsonl`: only Dream edits them
  (templates/agent/identity.md:18).
- Build, CI and packaging files (`pyproject.toml`, `uv.lock`, `.github/`, `scripts/`, `Dockerfile.test`).

## How to check a change is safe

Run from the repo root. Tests run in Docker (`scripts/test-docker.sh` builds the image from `nanobot/`, `tests/`
and `scripts/`; files outside the repo, such as workspace skills, are not in the image).

1. Focused tests for the touched area:
   - templates / prompt assembly: `scripts/test-docker.sh pytest tests/agent/test_context_builder.py tests/agent/test_dream.py tests/agent/test_consolidator.py tests/agent/test_subagent.py tests/agent/test_context_prompt_cache.py -q`
   - skills: `scripts/test-docker.sh pytest tests/agent/test_skills_loader.py tests/agent/test_skill_creator_scripts.py tests/agent/test_builtin_weather_skill.py tests/command/test_skill_command.py -q`
   - tools: `scripts/test-docker.sh pytest tests/tools tests/agent/test_tool_loader_scopes.py -q`
   - config / providers: `scripts/test-docker.sh pytest tests/config tests/providers -q`
2. Tool-name pin: `scripts/test-docker.sh pytest tests/agent/test_registered_tool_names.py -q` (the exact registered
   tool set, tests/agent/test_registered_tool_names.py:22-43).
3. Tool description pins: `scripts/test-docker.sh pytest tests/tools/test_tool_descriptions.py -q` (required phrases,
   exact strings and length caps for read_file, find_files, grep, edit_file, write_file, apply_patch, exec,
   exec_session, list_exec_sessions).
4. Removed-feature scan: `scripts/test-docker.sh pytest tests/agent/test_prompts_no_removed_features.py -q` (no
   template or skill may mention cron, heartbeat, the `message` tool, CLI apps, WebUI, gateway or chat channels).
5. Skill format: `python nanobot/skills/skill-creator/scripts/quick_validate.py <skill-dir>` must print
   "Skill is valid!" (needs Python 3 + PyYAML; for a repo skill:
   `scripts/test-docker.sh python nanobot/skills/skill-creator/scripts/quick_validate.py nanobot/skills/<name>`).
   The skill loader itself does not validate workspace or built-in skills (doc 03 section 2.2), so this script is
   the only format check.
6. Config: `python -c "from pathlib import Path; from nanobot.config.loader import load_config; load_config(Path('<config.json>'))"`
   must not raise `ConfigLoadError` (doc 04 section 1).
7. Kernel invariants: `scripts/test-docker.sh pytest tests/kernel -q`. The AST guard
   (tests/kernel/test_no_ambient_reads.py) fails on any `os.environ`/`getenv`, `Path.home()`, `expanduser("~")`,
   `expandvars` or `load_config`/`get_*_dir` call outside `nanobot/cli/`, `nanobot/config/`,
   `nanobot/kernel/legacy.py` and `nanobot/utils/restart.py`, and names the file and line. Route a new fallback
   through a `nanobot/kernel/legacy.py` helper instead of widening the allow-list.
8. Before handing a change back: `scripts/test-docker.sh` (full suite, expect 0 failed) and
   `scripts/test-docker.sh ruff check nanobot/ tests/`. Never run `ruff format`.
