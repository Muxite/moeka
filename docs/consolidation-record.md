# Consolidation record (003)

Per top-level path: where it came from and why. Base = `core-slim` `c382d0c9`; branch `consolidate/new-main`;
`main` `54069bb2` is merged in (`-s ours`, content already carried) so `main` fast-forwards to this branch.
Rules (spec FR-005): kernel-owned paths keep `core-slim`; gateway-owned paths come from `main`; a shared file is
hand-merged with kernel behaviour kept and gateway hooks restored behind a seam.

| Path | Source | Why |
|---|---|---|
| `moeka/`, `nanobot/kernel/`, `nanobot/llm_usage/`, `nanobot/security/`, `nanobot/core/`, `nanobot/api/complete.py` | `core-slim` | the kernel and its deprecated facade (authoritative) |
| `specs/`, `.specify/`, `.agent/` design docs, `docs/core-map/`, `docs/python-sdk.md`, `docs/migration-moeka-api.md`, `examples/` | `core-slim` | kernel design, specs, SDK docs |
| `.agent/deploy-runbook.md` | `main` | the one commit `main` has since the merge base (`54069bb2`) |
| `nanobot/channels/`, `nanobot/pairing/`, `nanobot/audio/`, `nanobot/optional_features.py`, `tests/channels`, `tests/pairing` | `main` | chat channels, pairing, audio (gateway host) |
| `nanobot/webui/`, `nanobot/web/`, `webui/`, `tui/`, `packages/`, `hatch_build.py`, `nanobot/cli/{webui,webui_support,desktop_*,tui_launcher,windows_browser}.py`, `nanobot/session/webui_turns.py`, `nanobot/cron/webui_metadata.py` | `main` | WebUI and native TUI |
| `nanobot/gateway/`, `nanobot/cron/`, `nanobot/triggers/`, `nanobot/apps/`, `nanobot/agent/{cron_turns}.py`, `nanobot/agent/tools/{message,cron,cli_apps}.py`, `nanobot/utils/evaluator.py`, cron skill, heartbeat/automation templates | `main` | gateway services and host-owned tools |
| `nanobot/api/{server,runtime}.py` | `main` | HTTP / openai-compat server (the kernel API is `complete.py`) |
| `nanobot/cli/{gateway,gateway_runtime,models,onboard,commands,agent,entry,runtime_config,terminal}.py`, `render.yaml`, `render-config.json` | `main` | gateway CLI and deploy files |
| `bin/moeka.sh`, `bin/bootstrap.sh`, `.github/workflows/*` (ci, tui-release), `Dockerfile.test`, `scripts/test-docker.sh` | `main` (+ `moeka/`, `examples/`, `docs/python-sdk.md` COPY lines, wall-clock cap added) | live service launcher and CI |
| `pyproject.toml`, `uv.lock` | `main`, plus kernel build lines (`moeka` package, tool description `.txt` files) | `main`'s lock already resolves every dependency; `uv lock --check` clean; no dependency added or removed by the kernel |
| `nanobot/agent/loop.py`, `subagent.py`, `config/schema.py`, `session/recovery.py`, `session/automation_turns.py`, `agent/tools/{context,self,cron,cli_apps}.py`, `agent/context.py`, `agent/skills.py`, `utils/{restart,__init__}.py`, `config/{loader,paths,__init__}.py`, `command/builtin.py`, `templates/agent/{identity,tool_contract}.md`, `conftest.py` | hand merge | kernel version + gateway hooks restored; see the seam list in `plan.md` |
| every other shared runtime file (providers, other tools, memory, sessions, sdk, nanobot.py, ...) | `core-slim` | kernel changes only, no gateway coupling found |
| `CLAUDE.md`, `AGENTS.md`, `docs/README.md`, `docs/my-tool.md` | reconciled | one description of the consolidated layout (FR-006) |
| tests | `core-slim` for kernel, `main` for host; see `tasks.md` T011 for the changed ones | |
