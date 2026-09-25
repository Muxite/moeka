# 03 - Prompts, Skills, Memory (reference for an automated editor)

Scope: everything that shapes what the model reads (templates, skills, memory files) plus the code that loads
them, on the `core-slim` branch. Citation format is `path:line`, relative to `nanobot/` unless the path starts
with `tests/` or `.agent/` (repo root). "unverified" marks anything not confirmed by reading code.

Editing rule of thumb: you (the editor agent) may change skills, prompt templates, tool descriptions/schemas
and config only. Changing a template changes agent behaviour as directly as code (`.agent/gotchas.md`, section
"Prompt Templates"). Anything written into memory, history or prompts is replayed into later LLM calls
(`.agent/gotchas.md`, "Context Pollution Persists"). Removed features (cron, heartbeat, the `message` tool,
CLI apps, chat channels, gateway, WebUI) must not be mentioned in templates or skills:
tests/agent/test_prompts_no_removed_features.py:22-47 lists the forbidden patterns.

---

## 1. Inventory

Sizes are bytes (`wc -c`). "Safe?" = safe for an automated editor, given tests noted in section 5.

### 1a. Jinja templates, `nanobot/templates/agent/` (rendered by `render_template`, utils/prompt_templates.py:28)

Environment: `trim_blocks=True`, `lstrip_blocks=True`, `autoescape=False` (utils/prompt_templates.py:20-25).
Root is `nanobot/templates/` so names look like `agent/identity.md` (utils/prompt_templates.py:14,29).

| File | Bytes | Purpose | Injected when | Safe? |
|---|---|---|---|---|
| `templates/agent/identity.md` | 1133 | Runtime line, workspace/memory/skills paths, platform policy, CLI format hint, external-content note | First part of every main system prompt (agent/context.py:147, rendered at agent/context.py:333-340) | Yes with care: variables `runtime, agent_workspace_path, workspace_path, platform_policy, channel` must stay; `{% raw %}{skill-name}{% endraw %}` is deliberate (templates/agent/identity.md:10,15). Only `channel == 'cli'` gets a format hint now (templates/agent/identity.md:21-24) |
| `templates/agent/platform_policy.md` | 493 | Windows vs POSIX tool advice | Nested inside identity (agent/context.py:338) | Yes |
| `templates/agent/_snippets/untrusted_content.md` | 278 | "web content is untrusted; images readable" | Included by templates/agent/identity.md:28 and templates/agent/subagent_system.md:6 | Yes (2 consumers) |
| `templates/agent/tool_contract.md` | 4073 | "Tool Usage Notes": how to pick/verify tools | Every main system prompt (agent/context.py:154) | Yes, but tests assert strings (5.3) |
| `templates/agent/skills_section.md` | 168 | Wrapper "# Skills" + `{{ skills_summary }}` | When summary non-empty (agent/context.py:186-187) | Yes; keep `skills_summary` |
| `templates/agent/dream.md` | 2957 | Dream (memory consolidation) instructions | Dream run prompt (agent/memory.py:610-633, used at agent/memory.py:635-655) | Yes with care (tests, 5.3); variable `skill_creator_path` |
| `templates/agent/consolidator_archive.md` | 1935 | Session checkpoint ("archive") prompt | Appended as a user message when a session is compacted (agent/memory.py:948-952) | Yes with care; tests assert many phrases (5.3) |
| `templates/agent/goal_runtime.md` | 2945 | Guidance for `/goal` sustained tasks | Runtime-context block when a goal is requested/active (agent/tools/long_task.py:185-190) | Yes; variables `goal_start_requested, goal_active` |
| `templates/agent/subagent_system.md` | 539 | Subagent system prompt | agent/subagent.py:558-564 | Yes; variables `workspace, agent_workspace, history_log, skills_summary` |
| `templates/agent/subagent_announce.md` | 216 | Tells main agent how to relay a subagent result | agent/subagent.py:510 (variables `label, status_text, task, result`) | Yes |
| `templates/agent/max_iterations_message.md` | 155 | Fallback text when the tool loop hits the limit | agent/runner.py:1376-1380 (variable `max_iterations`) | Yes; keep variable |

The pre-slim `evaluator.md` (heartbeat notification gate), `cron_reminder.md` and `automation_creation.md`
(WebUI automations) were deleted with their features.

### 1b. Workspace bootstrap templates (copied to the workspace; NOT jinja-rendered)

`sync_workspace_templates` copies every top-level `*.md` in `nanobot/templates/` plus `memory/MEMORY.md` and
`prompts/README.md` into the workspace, never overwriting existing files, and creates empty
`memory/history.jsonl` and `skills/` (utils/helpers.py:897-924). It then runs `GitStore.init()` for
`SOUL.md`, `USER.md`, `memory/MEMORY.md` (utils/helpers.py:932-946). In the slim core its only caller is the
CLI `nanobot agent` command (cli/agent.py:111); `MoekaCore` does not call it (grep). Consequence: editing a
template does NOT change an existing workspace copy, and a MoekaCore workspace only has these files if
something else created them.

| File | Bytes | Purpose | Injected when | Safe? |
|---|---|---|---|---|
| `templates/SOUL.md` | 410 | Default persona ("I am nanobot"), 5 principles | System prompt as `## SOUL.md` if workspace copy exists (agent/context.py:394-408) | Yes (persona text) |
| `templates/legacy/SOUL.md` | 805 | Old default SOUL; used only for detection | If workspace SOUL.md equals it, the newer `templates/SOUL.md` is substituted (agent/context.py:397-401) | No: must stay byte-identical to what old workspaces contain |
| `templates/USER.md` | 842 | Blank user-profile form | Skipped when workspace copy equals template (agent/context.py:404-407) | Yes, but any edit makes old unedited copies count as "customised" and get injected |
| `templates/AGENTS.md` | 326 | Workspace guidance (where to keep facts) | Same skip-if-default logic (agent/context.py:103,404-407) | Same caveat as USER.md |
| `templates/memory/MEMORY.md` | 408 | Empty long-term memory scaffold | Skipped from prompt if identical to workspace copy (agent/context.py:173) | Yes |
| `templates/prompts/README.md` | 402 | Explains the `prompts/dream.md` override | Never injected | Yes |

The pre-slim `templates/HEARTBEAT.md` was deleted.

### 1c. Prompt text in Python (not in templates/)

| Location | What | Safe? |
|---|---|---|
| agent/context.py:295-323 `_behavioral_guidelines()` | "# Working style" block, injected into every main system prompt (agent/context.py:153) | Python edit (out of your edit scope). It tells the model to use `bg_shell`, which is not auto-loaded (agent/tools/bg_shell.py:271-275), so the instruction points at a normally missing tool |
| agent/context.py:199-204 | `[Archived Context Summary]` wrapper | Python; tests assert it (tests/agent/test_context_builder.py:388,406) |
| agent/context.py:156-162 | `# Current Project` block when the project workspace differs from the agent workspace | Python |
| agent/loop.py:2178-2187 | Planning-step system prompt and `[Planning note ...]` prefix (only with `planning: true`) | Python |
| agent/loop.py:1200-1205 | Goal nudge text | Python |
| `nanobot/skills/README.md` | Directory readme (not a skill: no SKILL.md) | Yes |

### 1d. Workspace prompt overrides (user-side files, `<workspace>/prompts/`)

- Path helper: `<workspace>/prompts/<name>.md` (utils/workspace_prompts.py:13-15).
- Cap: `WORKSPACE_PROMPT_MAX_CHARS = 32_000` (utils/workspace_prompts.py:10); longer text is truncated by
  `truncate_text` (utils/workspace_prompts.py:32). Missing, unreadable or empty file returns None so the default
  is used (utils/workspace_prompts.py:18-33).
- `prompts/dream.md` replaces the Dream template (agent/memory.py:603-633). The Dream prompt then gets
  `\n\n## Conversation History\n...` appended (agent/memory.py:651-654); tests/agent/test_dream.py:86-88
  assert that suffix survives an override. `/dream-prompt init` creates the file (command/builtin.py:468-489).
- The pre-slim `prompts/evaluator.md` override no longer has a consumer.

### 1e. Built-in skills, `nanobot/skills/` (10 dirs; sizes are SKILL.md bytes)

Discovery scans `nanobot/skills/<dir>/SKILL.md` (agent/skills.py:15,89-103). None has `always` (grep). The
pre-slim `cron` skill was removed.

| Skill | Bytes | Frontmatter (verbatim keys) | Purpose | Fits a general agent core? |
|---|---|---|---|---|
| `skill-creator` | 19109 (+scripts) | name, description | How to write/package skills; scripts `init_skill.py`, `package_skill.py`, `quick_validate.py` | Yes, essential for self-improvement. Very long; the Dream prompt tells the model to read it (templates/agent/dream.md:36) |
| `memory` | 879 | name, description | How to `grep` `history.jsonl` | Yes (tied to memory design) |
| `my` | 2677 (+`references/examples.md`) | name, description (long) | Inspect/adjust runtime state via the `my` tool | Yes (the `my` tool is registered by default, agent/loop.py:694-704) |
| `image-generation` | 2779 | name, description | `generate_image` tool guide | Optional (the tool is off by default) |
| `github` | 1374 | name, description, metadata (`requires.bins: [gh]`, install hints) | `gh` CLI recipes | Optional, dev-specific |
| `summarize` | 2040 | name, description, homepage, metadata (`requires.bins: [summarize]`) | External `summarize` CLI | Optional; listed as unavailable when the binary is missing |
| `tmux` | 4063 (+2 shell scripts) | name, description, metadata (`os: [darwin, linux]`, `requires.bins: [tmux]`) | Drive interactive TTYs | Server-management flavoured; optional. The `os` key is carried as data only; no code enforcing it was found (grep, **unverified** for plugins) |
| `weather` | 1852 | name, description, homepage, metadata (`requires.bins: [curl]`) | wttr.in / Open-Meteo | No: consumer/chat-bot feature, prune candidate (a test names it: tests/agent/test_builtin_weather_skill.py) |
| `clawhub` | 1384 | name, description, homepage, metadata (emoji only) | Search/install skills from ClawHub registry | Only if the registry workflow is wanted; external-network dependency |
| `update-setup` | 5082 | name, description | Wizard that writes a personal `update` skill under the agent workspace | No: nanobot-product specific, has Chinese trigger words (skills/update-setup/SKILL.md:3); tests pin its text (5.3) |

---

## 2. SKILL.md format

### 2.1 Layout

```
<skills-root>/<skill-name>/SKILL.md      required
<skills-root>/<skill-name>/scripts/      optional
<skills-root>/<skill-name>/references/   optional
<skills-root>/<skill-name>/assets/       optional
```
`<skills-root>` is `<workspace>/skills/` (source "workspace") or `nanobot/skills/` (source "builtin"). The loader
only requires a directory containing `SKILL.md`; the directory name is the skill name
(agent/skills.py:93-102). Extra files in the skill root are NOT rejected by the loader; they are rejected only
by the `quick_validate.py` script (skills/skill-creator/scripts/quick_validate.py:190-200: only `SKILL.md`,
`scripts/`, `references/`, `assets/` allowed).

### 2.2 Frontmatter parse

- Regex: opening `---`, YAML, closing `---` on its own line; CRLF ok (agent/skills.py:17-21). YAML via
  `yaml.safe_load`; invalid YAML or a non-dict returns None (agent/skills.py:26-36).
- `get_skill_description` returns frontmatter `description` if a non-empty string, else falls back to the skill
  NAME (agent/skills.py:342-348). So a broken/missing description makes the summary line show just the name, and
  the skill is still listed.
- The loader does NOT call `valid_skill_metadata` for workspace or builtin skills. In the slim core that function
  is used only for agent-plugin skills (agent/plugins.py:479). Still, follow its rules for every skill, and run
  `quick_validate.py` (2.8) because nothing else validates a workspace skill.

### 2.3 Validation rules

Loader-level "identity contract" (agent/skills.py:22,39-48):
- `name` must equal the directory name exactly.
- Name regex `^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` (lowercase letters, digits, single hyphens, no
  leading/trailing hyphen, no `--`), length <= 64.
- `description` must be a string, 1 to 1024 characters after strip.

`quick_validate.py` (stricter, the one to run; skills/skill-creator/scripts/quick_validate.py:16-25,102-129,158-188):
- Allowed frontmatter keys only: `name, description, metadata, always, license, allowed-tools`. Anything else fails.
- Name regex `[a-z0-9]+(?:-[a-z0-9]+)*`, max 64, equals folder name.
- Description: non-empty, no `TODO` / `[todo` text, NO angle brackets `<` or `>`, max 1024 chars.
- `always`, if present, must be a boolean (skills/skill-creator/scripts/quick_validate.py:186-188).
- Note: `skills/update-setup` and others use `<...>` in the BODY, which is fine; the angle-bracket ban applies to
  the description only.

### 2.4 metadata block (requires / always)

- `metadata` may be a YAML mapping or a JSON string; the payload is under key `nanobot` (or legacy `openclaw`)
  (agent/skills.py:359-377). Built-ins use single-line JSON:
  `metadata: {"nanobot":{"emoji":"🐙","requires":{"bins":["gh"]}}}` (skills/github/SKILL.md:4).
- `requires.bins` (list of executables on PATH via `shutil.which`) and `requires.env` (env vars that must be
  non-empty) (agent/skills.py:307-316,379-384). Wrong shapes are tolerated as empty (agent/skills.py:307-316).
  Unmet requirements do not remove the skill from the prompt summary; it is shown with
  `(unavailable: CLI: x, ENV: Y)` (agent/skills.py:293-295), but `list_skills(filter_unavailable=True)` and
  explicit `$name` resolution exclude it (agent/skills.py:151-152,199-202).
- `always: true` may be top-level or inside the `nanobot` payload (agent/skills.py:391-401). Always-skills have their
  full body (frontmatter stripped) placed in the system prompt under `# Active Skills` (agent/context.py:176-180)
  and are excluded from the summary (agent/context.py:182-185). No built-in skill uses it. Token cost is the whole
  body on every turn; avoid unless essential. `skills/skill-creator/SKILL.md:331` says to keep frontmatter minimal.
- `emoji`, `install`, `homepage`, `os` are carried as data; the loader code read does not act on them.

### 2.5 Minimal valid example

```markdown
---
name: disk-report
description: Report disk usage for a directory and list the largest subfolders. Use when the user asks what is filling a disk or folder.
---

# Disk report

1. Run `exec` with `du -h --max-depth=1 <path> | sort -h | tail -15`.
2. Summarise the top 5 entries in plain text.
3. If `du` is denied or times out, say so; do not retry the same command.
```
Saved as `<workspace>/skills/disk-report/SKILL.md`. (Example is illustrative, not an existing skill.)

### 2.6 How skills reach the model

1. Summary in system prompt: one line per skill `- **name** - description[ (unavailable: ...)]  \`relative/SKILL.md\``
   (agent/skills.py:302; the dash in code is an em dash), grouped under headings "Inline skills", "Workspace
   skills (`skills`)", "Agent Plugin skills", "Built-in skills (`skills`)" (agent/skills.py:259-286). Wrapper text:
   `templates/agent/skills_section.md`. The model must call `read_file` on root + relative path to load a body
   (progressive loading, agent/skills.py:241-242).
2. Explicit invocation: `$skill-name` in the user's message (regex `(?<![\w$])\$([A-Za-z0-9_-]+)`,
   agent/skills.py:23) injects the stripped body into that user turn as
   `[Active Skills - instructions for this user turn]` (agent/skills.py:210-230; used at agent/context.py:531-533).
   Always-skills are not duplicated (agent/skills.py:218-219).
3. Size: full descriptions of every listed skill are in every prompt (no truncation code in
   `build_skills_summary`, agent/skills.py:232-304). Keep descriptions short (1-2 sentences, under ~300 chars).
   The body is loaded only on demand, so put "when to use" in the description, not the body
   (skills/skill-creator/SKILL.md:323-330: "Include all 'when to use' information here").

### 2.7 Discovery order and precedence (agent/skills.py:105-153)

1. Workspace `skills/*/SKILL.md` (source `workspace`; skipped if an inline skill has the same name)
   (agent/skills.py:117-120).
2. Enabled agent-plugin skills (source `plugin`), skipped if the name already seen (agent/skills.py:121-133).
3. Built-in `nanobot/skills/*` (source `builtin`), skipped if the name already seen (agent/skills.py:134-137). So a
   workspace skill with the same directory name shadows the built-in.
4. If `allowed_skills` is a set, non-inline skills not in it are dropped (agent/skills.py:139-140).
5. Inline (in-memory, host-registered) skills are prepended; they shadow everything and bypass `allowed_skills`
   (agent/skills.py:142-145).
6. `disabled_skills` removes by name (agent/skills.py:147-149). (The pre-slim CLI-app skill aliases are gone.)
7. `filter_unavailable` drops unmet requirements (agent/skills.py:151-152).
`iterdir()` order is unsorted (agent/skills.py:93), so order within a group is filesystem order.

### 2.8 Testing that a new skill loads (read-only)

- Format check (the loader does not validate workspace skills, so always run this):
  `python nanobot/skills/skill-creator/scripts/quick_validate.py <skill-dir>` prints "Skill is valid!" and exits 0
  (skills/skill-creator/scripts/quick_validate.py:203-213). It needs only the standard library plus PyYAML. For a
  skill inside the repo you can run it in Docker:
  `scripts/test-docker.sh python nanobot/skills/skill-creator/scripts/quick_validate.py nanobot/skills/<name>`.
- Listing:
  `python -c "import pathlib,sys; from nanobot.agent.skills import SkillsLoader; l=SkillsLoader(pathlib.Path(sys.argv[1])); print(l.list_skills(filter_unavailable=False)); print(l.get_skill_metadata('disk-report')); print(l.get_skill_availability('disk-report')); print(l.build_skills_summary())" <workspace>`
  Success: the name is in the list with the right `source`, metadata is a dict with `description`, availability is
  `(True, '')`.
- Test suite: `scripts/test-docker.sh pytest tests/agent/test_skills_loader.py tests/agent/test_skill_creator_scripts.py tests/agent/test_prompts_no_removed_features.py -q`.

### 2.9 skill-creator skill (`nanobot/skills/skill-creator/`)

- Frontmatter: `name: skill-creator`, one-line description (skills/skill-creator/SKILL.md:1-4). 19109 bytes, i.e. a
  long read (~5k tokens); Dream is told to follow it (templates/agent/dream.md:36, path injected from
  agent/memory.py:613-617).
- Teaches: progressive disclosure levels (metadata always in context ~100 words, body under 5k words, resources on
  demand) (skills/skill-creator/SKILL.md:117-118); use `scripts/init_skill.py` to scaffold
  (skills/skill-creator/SKILL.md:269); frontmatter guidance (skills/skill-creator/SKILL.md:322-331).
- Scripts: `init_skill.py` (template description contains a `[TODO: ...]` placeholder at
  skills/skill-creator/scripts/init_skill.py:25 that quick_validate rejects until replaced), `package_skill.py`,
  `quick_validate.py`. Tests: tests/agent/test_skill_creator_scripts.py.
- Dream's format instruction differs slightly: "YAML frontmatter with name and description, under 2000 words,
  covering when to use it, steps, output format, and an example" (templates/agent/dream.md:36).

---

## 3. Assembly of the main system prompt, in order

Function: `ContextBuilder.build_system_prompt` (agent/context.py:133-206). Parts are joined with
`"\n\n---\n\n"` (agent/context.py:206). Doc 01 section 3 has the same table with editability notes.

| # | Part | Source | Condition |
|---|---|---|---|
| 1 | Identity: runtime, workspace paths, platform policy, CLI format hint, external-content notes | `templates/agent/identity.md` via `_get_identity` (agent/context.py:147,325-340) | always |
| 2 | Bootstrap files, each as `## <NAME>\n\n<content>`: `AGENTS.md` (from project root), `SOUL.md`, `USER.md` (from agent workspace) (agent/context.py:377-381); plus host in-memory overrides (agent/context.py:388-392,410-414) | Workspace files | Included only if non-empty; AGENTS.md/USER.md skipped when byte-equal (stripped) to bundled template (agent/context.py:103,404-407); SOUL.md replaced by new default if it equals legacy default (agent/context.py:397-401). Note `BOOTSTRAP_FILES` (agent/context.py:102) also lists the three names but the loop uses `sources` (agent/context.py:377). A `TOOLS.md` in the workspace is NOT loaded (not in `sources`) |
| 3 | `# Working style` | Hard-coded Python `_behavioral_guidelines` (agent/context.py:153,295-323) | always |
| 4 | `# Tool Usage Notes` | `templates/agent/tool_contract.md` (agent/context.py:154) | always |
| 5 | `# Current Project` working dir | Python (agent/context.py:156-162) | only if project workspace differs from agent workspace |
| 6 | `# Memory\n\n## Long-term Memory\n...` (MEMORY.md, or top-k semantic chunks if it exceeds `semantic_threshold` chars (default 2048) AND a VecStore is available) | `MemoryStore.get_memory_context` (agent/memory.py:265-301, agent/context.py:164-174) | `include_memory` and MEMORY.md non-empty and not equal to the template. Cosmetic quirk: `get_memory_context` already returns a `## Long-term Memory` heading (agent/memory.py:301) and agent/context.py:174 adds another, so the heading appears twice. `AgentLoop.from_config` passes no VecStore (agent/loop.py:542-573), so the full MEMORY.md is injected, uncapped, unless the host (e.g. `MoekaCore.from_config`) supplies one |
| 7 | `# Active Skills` full bodies of `always` skills | `SkillsLoader.get_always_skills` (agent/context.py:176-180) | only if any |
| 8 | `# Skills` summary | `templates/agent/skills_section.md` + `build_skills_summary` (agent/context.py:182-187) | if any skills |
| 9 | `# Recent History`: entries from `history.jsonl` newer than the Dream cursor, last `history_recent_k` (default 15) plus semantic extras, formatted `- [timestamp] content`, truncated to 8000 tokens | `_build_history_section` (agent/context.py:189-197,208-274, cap at agent/context.py:105,273) | `include_memory_recent_history` and entries exist. Per-session filtering: only the same `session_key` unless unified session (agent/memory.py:462-481) |
| 10 | `[Archived Context Summary]` previous-conversation checkpoint | `session_summary` (agent/context.py:199-204) | if present and not `(nothing)` |

Per-turn additions to the USER message (not the system prompt): runtime-context blocks and `$skill` blocks
(agent/context.py:518-540); goal guidance from `goal_runtime.md` is one such block (agent/tools/long_task.py:185-193).

Other prompts built separately:
- Subagent: `subagent_system.md` with its own skills summary (agent/subagent.py:550-564); no SOUL/USER/MEMORY are
  injected (per template content, templates/agent/subagent_system.md:1-20).
- Dream: `dream.md` + history batch, run with a restricted tool registry (4.3).
- Consolidator checkpoint: `consolidator_archive.md` appended as a user message onto the existing history
  (agent/memory.py:947-953).

---

## 4. Memory and Dream

### 4.1 Files (`agent/memory.py`, class `MemoryStore`, agent/memory.py:59)

| File | Path | Written by | Read by | Git-tracked |
|---|---|---|---|---|
| `SOUL.md` | `<workspace>/SOUL.md` (agent/memory.py:91) | Dream, user | system prompt (bootstrap) | yes |
| `USER.md` | `<workspace>/USER.md` (agent/memory.py:92) | Dream, user | system prompt | yes |
| `memory/MEMORY.md` | agent/memory.py:88 | Dream, user | system prompt (section 3 #6) | yes |
| `memory/.dream_cursor` | agent/memory.py:94 | `run_dream` after a completed Dream run (agent/dream.py:77-78) | `get_last_dream_cursor` (agent/memory.py:590-594) | yes (agent/memory.py:100-102) |
| `memory/history.jsonl` | agent/memory.py:89 | `append_history` (checkpoints) | prompt "Recent History", Dream, `grep` by the agent | NO |
| `memory/.cursor` | agent/memory.py:93 | cursor counter for history | `_read_cursor_counter` (agent/memory.py:421) | NO |
| `memory/HISTORY.md` | agent/memory.py:90 | legacy; migrated once to jsonl (agent/memory.py:118) | | NO |
| `AGENTS.md`, `skills/` | workspace | user/agent/Dream (skills only) | prompt | NO (not in the tracked list) |

Git tracking: `GitStore(workspace, tracked_files=[SOUL.md, USER.md, memory/MEMORY.md, memory/.dream_cursor])`
(agent/memory.py:100-102). Its `.gitignore` is `/*` plus `!` entries for tracked files/dirs
(utils/gitstore.py:213-227), so everything else in the workspace is ignored. `sync_workspace_templates`
initialises git with only the first three files (utils/helpers.py:936-944); the fourth is added via the store's
own tracked list/gitignore update (utils/gitstore.py:84-87, partly read). `GitStore` API (summary): `init`,
`auto_commit(message)` (stage tracked files, commit if changed, return short SHA; utils/gitstore.py:125-133),
`log`, `diff_commits`, `summarize_working_tree`, `show_commit_diff`, `revert(commit)` (utils/gitstore.py:230-531
def list). It uses dulwich (utils/gitstore.py:133-134). Users undo memory edits with `/dream-restore <sha>` and
inspect them with `/dream-log` (command/builtin.py:609,672).

Important: skills under `<workspace>/skills/` are NOT git-tracked, so a bad skill edit has no automatic revert. Use
your own backup (**unverified** whether a given workspace has another VCS).

### 4.2 history.jsonl

- Entry shape `{"cursor": int, "timestamp": str, "content": str}` plus `session_key` (agent/memory.py:362,
  agent/context.py:289). Cursors auto-increment (agent/memory.py:431-447). Content hard cap 64,000 chars per
  entry (agent/memory.py:844, used at agent/memory.py:312).
- Rotation: `compact_history` keeps at most `max_history_entries` (default 1000, agent/memory.py:62), but never
  drops entries newer than the Dream cursor (agent/memory.py:483-511). `run_dream` calls it after every run
  (agent/dream.py:104).
- Writes are atomic (`_write_entries`, agent/memory.py:563; `.agent/gotchas.md` "Atomic Session Writes").
- Entries are created by the Consolidator/`MemoryArchiver`: when a session is compacted, the model is asked
  (using `consolidator_archive.md`) for `- [mark] fact` lines; the result is appended unless it equals `(nothing)`
  (agent/memory.py:1085-1087); if the LLM call fails or does not fit the token budget a raw dump
  (`_RAW_ARCHIVE_MAX_CHARS = 16_000`, agent/memory.py:843) is stored instead (`raw_fallback`, agent/memory.py:936,
  returned at agent/memory.py:978,1000,1021,1066-1084). Raw dumps land in history and later feed Dream and the
  "Recent History" prompt: this is the main pollution vector.
- The LLM calls for archive are tagged `llm_usage_source("dream")` (agent/memory.py:1005), so usage stats label
  consolidation as "dream".

### 4.3 Dream run (`agent/dream.py`, invoked directly)

Core has no scheduler. A run happens only when a caller awaits `run_dream(loop)` (agent/dream.py:41-107): the
`/dream` command (command/builtin.py:427-465), `AgentLoop.run_dream()` (agent/loop.py:256-260), or a
harness/host. Flow:
1. `store.build_dream_prompt()` reads entries with `cursor > .dream_cursor`, takes at most 20 (`max_entries=20`),
   each content truncated to 1000 chars (agent/memory.py:650), and returns
   `template + "\n\n## Conversation History\n[ts] text..."` plus the last cursor (agent/memory.py:635-655). If it
   returns None the result is `status="no_input"` (agent/dream.py:60-62).
2. The current contents of SOUL/USER/MEMORY reach Dream only through the normal system context, not the prompt
   (docstring agent/memory.py:641-643; test tests/agent/test_dream.py:65-72 asserts no "## Current Memory Files"
   section).
3. Runs `loop.process_direct(prompt, session_key=dream:<timestamp>, ephemeral=True, tools=store.build_dream_tools(),
   runtime=loop.dream_runtime())` (agent/dream.py:65-72; session key agent/memory.py:791-794;
   `dream_runtime` uses `agents.defaults.dream.modelOverride`, agent/loop.py:250-254). The Dream tools are
   `read_file` (workspace + read access to built-in skills dir), and `edit_file`, `apply_patch`, `write_file` whose
   write area is `<workspace>/skills/` plus exactly three extra files: MEMORY.md, SOUL.md, USER.md
   (agent/memory.py:667-708). So Dream can NOT edit AGENTS.md, history.jsonl, or code.
4. Only if the run completed normally (`dream_run_completed`, agent/memory.py:711-719) is the cursor advanced
   (agent/dream.py:77-84); otherwise the status is `incomplete` and the same batch is retried by the next call
   (agent/dream.py:85-91). An exception gives `status="failed"` (agent/dream.py:92-98).
5. Whatever the outcome: `auto_commit` with a diff-grounded message built from `commit_prefix` (default
   "dream: manual run") when the memory git store is initialised, `compact_history()`, `reindex_memory()`, and
   pruning of old Dream sessions keeping 10 (agent/dream.py:99-106; agent/memory.py:796,814).
6. Config: `DreamConfig` = `enabled` (default True), `interval_h` (default 2, ge=1), `model_override` (aliases
   `modelOverride`, `model`) (config/schema.py:30-45). `enabled` and `interval_h` are advisory: nothing in
   `nanobot/` reads them (grep); `/dream` runs regardless. A retired `dream.cron` key is dropped with a warning
   (config/schema.py:46-62). Nothing advances the cursor when Dream is not run, so unprocessed history accumulates
   until someone runs Dream. There is no guard against two overlapping `run_dream` calls; the caller must
   serialize them.
7. Only Dream may edit profile/memory files: stated to the main agent in templates/agent/identity.md:18.

Dream's own rules (read `templates/agent/dream.md`): file routing table (templates/agent/dream.md:5-12), tags
`[skip] [correction] [permanent] [durable] [ephemeral]` (templates/agent/dream.md:16-24), "always strip bracketed
tags from saved memory" (templates/agent/dream.md:26), create a skill only when a workflow appeared at least twice
(templates/agent/dream.md:32).

### 4.4 Consolidator / archive prompt

`consolidator_archive.md` asks for a replacement checkpoint, SNIP criteria (Signal, Novel, Important, Persistent,
templates/agent/consolidator_archive.md:23-27), marks `[permanent] [durable] [ephemeral] [correction]`
(templates/agent/consolidator_archive.md:29-33) and output lines `- [mark] fact` or `(nothing)`
(templates/agent/consolidator_archive.md:39-42). `Consolidator` coordinates locks per session and token budget
(agent/memory.py:1165-1200,1266-1300). Token thresholds live in code/config (**unverified** detail).

### 4.5 Blast radius of a bad edit

- `SOUL.md` / `USER.md` / `AGENTS.md`: injected verbatim into EVERY main-agent system prompt
  (agent/context.py:365-416). A wrong rule, a giant paste, or prompt-injection text persists across all sessions
  until manually reverted. SOUL/USER/MEMORY also get rewritten by Dream from history, so a polluted history line
  can be re-copied into memory repeatedly.
- `MEMORY.md`: injected in full when no VecStore is present (agent/memory.py:281-301), so growth costs tokens on
  every turn. Empty or template-identical content is skipped (agent/context.py:173).
- Bad frontmatter in a skill: skill still listed but with the skill name as description (agent/skills.py:342-348),
  so the model cannot tell when to use it. Non-directory or SKILL.md missing: silently ignored
  (agent/skills.py:94-98).
- A bad Dream override `prompts/dream.md`: affects all future Dream runs; deleting or emptying the file restores the
  default (templates/prompts/README.md:13).
- Backups: the memory files are recoverable via git (`/dream-restore`); skills and AGENTS.md are not.
- Do not put local paths, timestamps, tool-call echoes, or raw fallback dumps into examples in prompts or skills
  (`.agent/gotchas.md`, "Prompt Templates").

---

## 5. Style conventions and pitfalls

### 5.1 Conventions seen in existing prompts

- Second person imperative, terse bullets, backticked tool names (`tool_contract.md`, `dream.md`). Sections use `##`
  headings; the model-facing wrappers use bracketed tags like `[Goal Runtime Guidance - host instructions]` ...
  `[/Goal Runtime Guidance]` (templates/agent/goal_runtime.md:1,31) and `[Subagent '...' ...]`
  (templates/agent/subagent_announce.md:1).
- Skills: H1 title, short bullets/tables, runnable command or tool-call examples in fenced blocks (e.g.
  `skills/github/SKILL.md`), descriptions written as "what it does. Use when ...".
- Persona (`SOUL.md`) is first person ("I am nanobot", templates/SOUL.md:3); everything else is second person or
  imperative.
- Tool names must match the registry exactly. `tool_contract.md` names `find_files`, `list_dir`, `grep`, `read_file`,
  `apply_patch`, `edit_file`, `write_file`, `exec`, `exec_session`, `list_exec_sessions`, `web_search`, `web_fetch`
  (templates/agent/tool_contract.md:21-61); all are in the pinned tool set (doc 02). Before adding a tool name in
  prose, check tests/agent/test_registered_tool_names.py:22-43.
- Skills reference paths agent-relative or with placeholders like `<history-log-path>` and `<agent-workspace>`
  (tests/agent/test_skills_loader.py:347-357); never hard-code a project-relative `memory/history.jsonl` in a skill.

### 5.2 Jinja pitfalls

- Variables in `{{ }}` and blocks in `{% %}` are rendered; a missing variable renders as empty (Jinja default
  `Undefined`; `StrictUndefined` is not set, utils/prompt_templates.py:20-25), so removing a variable from a
  caller-vs-template pair fails silently.
- Literal braces: `{skill-name}` is wrapped in `{% raw %}` in templates/agent/identity.md:10,15. Any new literal
  `{{` or `{%` in a template must be wrapped in `{% raw %}` or it will be interpreted (or raise).
- `trim_blocks` + `lstrip_blocks` mean block tags on their own line vanish including the newline; keep tags on their
  own lines.
- `max_iterations_message.md` is rendered by Jinja with the variable `max_iterations` (agent/runner.py:1376-1380).
  Separately, if a caller sets `AgentRunSpec.max_iterations_message` (agent/runner.py:139), that string is used
  instead and passed through Python `str.format(max_iterations=...)` (agent/runner.py:1372-1375), so such a string
  must not contain stray braces. The only in-tree caller sets a fixed string without braces
  (agent/subagent.py:443); there is no config key for it.
- Non-jinja markdown files (SOUL/USER/AGENTS/MEMORY) are copied raw; `{{ }}` in them is NOT rendered.
- `dream.md` requires `skill_creator_path` (agent/memory.py:613-617); `consolidator_archive.md` is rendered with
  `archive_count` (agent/memory.py:948-952) though the template does not use it.

### 5.3 What tests assert on (do not break)

- tests/agent/test_context_builder.py:282-284 asserts the system prompt contains `# Tool Usage Notes`,
  `## General Tool Contract`, and "Do not use `exec` as a universal workaround". tests/agent/test_context_builder.py:263-276
  checks the template content, including `## Media` present and `## Messaging and Media` / `## Scheduling and
  Background Work` absent. Lines 388, 400, 406: `[Archived Context Summary]` and the `"\n\n---\n\n"` separator.
  Lines 480, 491: `[Active Skills - instructions for this user turn]` in the user prompt, `# Active Skills` absent
  for explicit skills.
- tests/agent/test_consolidator.py:399-415 (`TestConsolidatorPromptContract`): the archive prompt must contain
  `## Merge rules`, `## What to retain`, `## Output`, "replacement checkpoint", `[Archived Context Summary]`,
  "current conversation state", "SNIP", all four marks `[permanent] [durable] [ephemeral] [correction]`,
  "working-state handoff", "- [mark] fact", "(nothing)"; it must NOT contain `[skip]` or `history.jsonl`.
- tests/agent/test_dream.py: prompt contains `## Conversation History` (tests/agent/test_dream.py:34), "skill-creator"
  (tests/agent/test_dream.py:63), no "## Current Memory Files" (tests/agent/test_dream.py:70), override handling
  (tests/agent/test_dream.py:74-100). tests/agent/test_dream_direct.py and tests/command/test_builtin_dream.py
  cover `run_dream` and `/dream` (contents not read line by line).
- tests/agent/test_skills_loader.py: bundled `update-setup` description must be valid YAML starting "One-time setup
  wizard" and containing "Triggers:" (tests/agent/test_skills_loader.py:339-344); `memory` skill must contain
  `<history-log-path>` and not `path="memory/history.jsonl"`; `update-setup` must contain
  `<agent-workspace>/skills/update/SKILL.md` and "Never substitute a project-relative"
  (tests/agent/test_skills_loader.py:347-357). Deleting a built-in skill breaks tests that name it, e.g.
  tests/agent/test_builtin_weather_skill.py.
- tests/agent/test_prompts_no_removed_features.py: no template or skill may match the forbidden patterns (cron,
  heartbeat, message tool, CLI apps, webui, gateway, chat-channel names, "channel") except the two documented
  exceptions (tests/agent/test_prompts_no_removed_features.py:22-58); the Python prompt sources it scans must not
  mention the `message` tool (tests/agent/test_prompts_no_removed_features.py:89-105).
- Other files referencing templates/skills (grep hit list, contents not read line by line):
  tests/agent/test_subagent.py, tests/agent/test_agent_plugins.py, tests/agent/test_context_prompt_cache.py,
  tests/agent/test_new_command_archival.py, tests/command/test_skill_command.py. Run these after any template/skill
  edit.
- Prompt-cache note: tests/agent/test_context_prompt_cache.py exists; the system prompt is expected to be stable turn
  to turn, so avoid adding timestamps or random text to system-prompt templates.

Suggested check after edits:
`scripts/test-docker.sh pytest tests/agent/test_context_builder.py tests/agent/test_dream.py tests/agent/test_consolidator.py tests/agent/test_skills_loader.py tests/agent/test_subagent.py tests/agent/test_prompts_no_removed_features.py -q`,
then `scripts/test-docker.sh ruff check nanobot/ tests/` for Python (no `ruff format`, `.agent/gotchas.md`).

---

## 6. Remaining product-flavoured content (candidates to prune or generalise; NOT changed)

The slim removed the chat-channel, cron, heartbeat, evaluator, CLI-app and WebUI prompt text (templates and
skills are now checked by tests/agent/test_prompts_no_removed_features.py). What is left:

Chat/consumer flavoured:
- `templates/agent/subagent_announce.md:8` ("Summarize this naturally for the user ...") assumes a human reader.
- agent/context.py:295-323 working-style block: co-worker tone plus `bg_shell` instructions for a tool that is not
  registered (Python).
- Skills: `weather`, `update-setup` (nanobot upgrade wizard, Chinese triggers, skills/update-setup/SKILL.md:3),
  `clawhub`, `image-generation`.
- `SOUL.md` template: emoji persona "nanobot" (templates/SOUL.md:3); `legacy/SOUL.md` has execution rules that
  conflict slightly with tool_contract ("wait for user confirmation" at templates/legacy/SOUL.md:16 versus "outline
  the plan briefly and then execute it", templates/agent/tool_contract.md:13).
- User-facing strings in command/builtin.py still mention the removed WebUI and scheduler
  (command/builtin.py:518,621) (Python; out of scope).

Server-management / homelab specific:
- Skills `tmux`, `github`.
- agent/context.py:306-308 mentions dd, image flashing, rsync, package installs (Python).
- `tool_contract.md` "Process Execution" (`exec`, `exec_session`, `yield_time_ms`,
  templates/agent/tool_contract.md:47-51) is general enough to keep.

General-core keepers: `tool_contract.md` sections General Tool Contract, Discovery and Reading, File and Coding
Workflows, Web; `dream.md`; `consolidator_archive.md`; `skills_section.md`; `subagent_system.md`;
`goal_runtime.md`; skills `skill-creator`, `memory`, `my`.

---

## 7. Re-verification log

Re-verified on `core-slim` (commit 119e0063 plus docs). Method: a throwaway checker extracted every `path:line`
and `path:line-line` citation in this file, resolved the path, confirmed the line is inside the file, and printed
the cited line(s); each printed line was compared with the claim by eye, and every mismatch was re-located with
grep. Result: all citations in this file resolve and match (0 dangling, 0 marked unverified).

What changed against the pre-slim version of this doc:
- Removed (feature deleted): `evaluator.md`, `cron_reminder.md`, `automation_creation.md`, `HEARTBEAT.md`,
  `prompts/evaluator.md`, the `cron` skill, the heartbeat preamble and evaluator tool schema rows, the gateway Dream
  flow (`cli/gateway_runtime.py`), section 5.4 (heartbeat file pitfall), the evaluator tests, and the
  channel/cron/heartbeat items in section 6. Machine-specific observations about the owner's live `~/.nanobot`
  were dropped.
- Rewritten: section 4.3 now describes `agent/dream.py:run_dream` (direct invocation, advisory `DreamConfig`);
  the `.dream_cursor` writer is `run_dream`, not the gateway; `valid_skill_metadata` is now used only by agent
  plugins (CLI apps are gone); skill discovery lost the CLI-app alias step.
- Known errors fixed: the `# Current Project` block is at agent/context.py:156-162 (was cited as 163-165 / 158-164);
  the section 5.2 `max_iterations` bullet now says that only a caller-supplied `AgentRunSpec.max_iterations_message`
  goes through `str.format` while the template is rendered by Jinja; quick_validate's `__main__` block ends at line
  213 (was cited as 203-215); `identity.md`, `tool_contract.md` and `AGENTS.md` line ranges were shortened to the
  slimmed files; section 7 (this log) was empty.
- Re-located (file edited by the slim): agent/context.py (-2 to -5 lines), agent/skills.py (CLI-app alias code
  removed), command/builtin.py, config/schema.py, the templates. Files unchanged since the doc was first written
  (agent/memory.py, utils/helpers.py, utils/gitstore.py, utils/workspace_prompts.py, the skill-creator files, most
  tests) kept their line numbers, and those citations were re-checked the same way.
