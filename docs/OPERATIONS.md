# Moeka operations guide

Moeka runs natively on the host via a Python venv managed by UV.

## First-time setup

```sh
# 1. Copy secret template and fill in real keys
cp keys.env.example keys.env
$EDITOR keys.env          # DO NOT commit keys.env

# 2. Install dependencies (creates ./.venv)
./bin/moeka.sh install

# 3. Verify
./bin/moeka.sh doctor
```

## Day-to-day

```sh
./bin/moeka.sh start          # run the gateway
./bin/moeka.sh status         # workspace, config, running PID
./bin/moeka.sh logs -f        # tail output
./bin/moeka.sh restart
./bin/moeka.sh stop
./bin/moeka.sh shell          # drop into the venv
./bin/moeka.sh exec -- <cmd>  # run a nanobot subcommand
```

## Boot setup

```sh
./bin/moeka.sh enable         # install + enable systemd user service
./bin/moeka.sh disable        # stop + disable service

# For headless servers (keep service running after logout):
loginctl enable-linger "$USER"
```

## Directory layout

Moeka uses a single instance directory — config, agent identity, runtime
state, and media all live under `$MOEKA_WORKSPACE` (default `~/.nanobot`).

| Path                          | Role                                                          |
|-------------------------------|---------------------------------------------------------------|
| `~/.nanobot/config.json`      | Active instance configuration (uses `${ENV_VAR}` placeholders)|
| `~/.nanobot/SOUL.md` etc.     | Agent identity (SOUL / AGENTS / HEARTBEAT / TOOLS / USER)     |
| `~/.nanobot/skills/`          | User-authored skills                                          |
| `~/.nanobot/memory/`          | Vector memory + dream history                                 |
| `~/.nanobot/sessions/`        | Per-channel conversation state                                |
| `~/.nanobot/media/`           | Channel attachments / exports                                 |
| `~/.nanobot/cron/`            | Scheduled job registry                                        |
| `~/.nanobot/tool-results/`    | Persisted overflow from big tool outputs                      |
| `./keys.env`                  | Secrets (gitignored). Source of truth for `${VAR}` in config. |
| `./.env`                      | Non-secret per-host overrides (also gitignored)               |
| `./bin/moeka.sh`                  | Universal launcher (`bin/moeka.sh`)         |
| `./scripts/moeka.service`     | systemd user unit                                            |

## Multiple agents

Each agent is a single directory. Name two paths and you get two agents:

```sh
MOEKA_WORKSPACE=~/agents/alice ./bin/moeka.sh start
MOEKA_WORKSPACE=~/agents/bob   ./bin/moeka.sh start   # different terminal
```

The directory may itself be a git repo. Typical allowlist: `config.json`,
`SOUL.md`, `AGENTS.md`, `HEARTBEAT.md`, `TOOLS.md`, `USER.md`, `skills/`,
`memory/MEMORY.md`. Typical gitignore: `keys.env`, `history/`, `media/`,
`sessions/`, `tool-results/`, `config.json.bak.*`.

## Secrets flow

```
keys.env   (gitignored)
  |  sourced by moeka.sh
  v
process env
  |  read by nanobot config loader
  v
config.json (tracked placeholders like "${OPENROUTER_API_KEY}")
  |  resolve_config_env_vars()
  v
live Config object
```

## Permissions: non-sudo and sudo

### Non-sudo (default)

Moeka runs as the current user with no elevated privileges. It can read,
write, and execute anything the user can.

### Sudo (opt-in)

Two things must be in place:

1. **Config**: `tools.exec.allowSudo: true` in `config.json`
2. **Host policy**: sudo must allow the Moeka process to run the intended elevated commands

When disabled, the exec tool blocks commands containing `sudo` with one clear
error. When enabled, sudo commands run directly through the normal exec safety
guards: dangerous command patterns, workspace restrictions, sandbox wrapping,
internal URL blocking, timeouts, and output limits.

Check status with `./bin/moeka.sh doctor`.

## systemd

See [SYSTEMD.md](./SYSTEMD.md). `./bin/moeka.sh enable` installs and starts
the service. `./bin/moeka.sh disable` stops and disables it.

## Portability (export / import / new)

Moeka's state lives entirely in `$MOEKA_WORKSPACE`. Four commands cover
the full lifecycle of moving or duplicating an instance.

### Export

```sh
./bin/moeka.sh export                 # default: identity + skills + memory
./bin/moeka.sh export --with-sessions # also include per-channel chat history
./bin/moeka.sh export --with-media    # also include media/ (attachments)
./bin/moeka.sh export --anonymize     # wipe USER.md + allowFrom (for seeding others)
./bin/moeka.sh export --out /tmp/x.tar.gz
```

Always included: `SOUL.md`, `USER.md`, `AGENTS.md`, `HEARTBEAT.md`,
`TOOLS.md`, `config.json` (env-var placeholders intact, no resolved
secrets), `skills/`, `memory/`, `cron/`.

Always excluded: `keys.env`, `.env`, `*.log`, `moeka.pid`, `gateway.lock`,
`tool-results/`, `bg-shell/`, `config.json.bak.*`, sqlite WAL/SHM files.

### Import

```sh
./bin/moeka.sh import moeka-export-host-20260516.tar.gz
./bin/moeka.sh import x.tar.gz --workspace ~/.moeka-staging --force
```

Refuses to overwrite a non-empty workspace unless `--force`. Warns if
`config.json` references env vars that aren't set in the current
environment / `keys.env`.

### New identity

```sh
./bin/moeka.sh new alice                  # -> ~/.moeka-alice
./bin/moeka.sh new bob --workspace /srv/bob
```

Copies the templates under `templates/workspace/` into the target,
substituting `{{NAME}}` placeholders in `SOUL.md` / `USER.md`. All
channels start disabled with empty `allowFrom` — wire them up via
`./bin/moeka.sh telegram-pair` or by editing `config.json`.

### Telegram pairing

```sh
./bin/moeka.sh telegram-pair
```

Prompts for a bot token, validates via `getMe`, writes
`TELEGRAM_TOKEN=...` into `keys.env`, polls `getUpdates` for ~2 minutes
waiting for your first message, appends your user id to
`channels.telegram.allowFrom` in `config.json`, and enables the channel.
Use any time tokens change.

## Bootstrap on a new Ubuntu 24.04 host

`./bin/bootstrap.sh` runs the full first-time setup interactively: installs
`uv` if absent, builds the venv, seeds `keys.env`, prompts for workspace
mode (import / new / onboard), offers Telegram pairing, and offers
systemd enable. Idempotent — re-running on an already-set-up host skips
completed steps.
