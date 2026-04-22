# Moeka operations guide

Moeka (a nanobot flavor) runs in one of two modes, controlled by a single
entrypoint: `./moeka.sh`.

| Mode     | When it's used                                               | Command                    |
|----------|--------------------------------------------------------------|----------------------------|
| direct   | default on hosts without Docker, or when `.dockerized` absent| `./moeka.sh start`         |
| docker   | `.dockerized` present **or** `--docker` / `MOEKA_MODE=docker`| `./moeka.sh --docker start`|

## First-time setup

```sh
# 1. Copy secret template and fill in real keys
cp keys.env.example keys.env
$EDITOR keys.env          # DO NOT commit keys.env

# 2. Install dependencies (creates ./.venv OR builds the docker image)
./moeka.sh install

# 3. Verify
./moeka.sh doctor
```

## Day-to-day

```sh
./moeka.sh start          # bring it up
./moeka.sh status         # where is it, what mode, is it alive?
./moeka.sh logs -f        # tail output
./moeka.sh restart
./moeka.sh stop
./moeka.sh shell          # drop into the venv / container
./moeka.sh exec -- <cmd>  # run a nanobot subcommand
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
| `./moeka.sh`                  | Universal entrypoint                                          |
| `./moeka.service`             | systemd user unit                                             |
| `./docker-compose.yml`        | Container topology (host network + pid, one bind-mount)       |

## Multiple agents (version-controlled)

Each agent is a single directory. Name two paths and you get two agents:

```sh
# alice lives here:
MOEKA_WORKSPACE=~/agents/alice ./moeka.sh start

# bob in a different terminal:
MOEKA_WORKSPACE=~/agents/bob   ./moeka.sh start
```

The directory may itself be a git repo. Typical allowlist: `config.json`,
`SOUL.md`, `AGENTS.md`, `HEARTBEAT.md`, `TOOLS.md`, `USER.md`, `skills/`,
`memory/MEMORY.md`. Typical gitignore: `keys.env`, `history/`, `media/`,
`sessions/`, `tool-results/`, `config.json.bak.*`. Because secrets live only
in `keys.env` at the repo root (not the instance dir), the instance tree is
safe to push to a private git remote.

## Secrets flow

```
keys.env   (gitignored)
  │
  │  sourced by moeka.sh (direct) or env_file (docker)
  ▼
process env
  │
  │  read by nanobot config loader
  ▼
config.json (tracked placeholders like "${OPENROUTER_API_KEY}")
  │
  │  resolve_config_env_vars()
  ▼
live Config object
```

## Host bridge (docker mode)

With `MOEKA_EXEC_ON_HOST=1` (set by `docker-compose.yml`), every `exec()` the
agent runs is prepended with `nsenter -t 1 -m -u -n -i -p --`, so commands like
`lsblk`, `docker ps`, and `systemctl --user` behave the same way inside the
container as on the host. Requires `pid: host` and `cap_add: SYS_ADMIN`.

## systemd

See [SYSTEMD.md](./SYSTEMD.md). `bash install-service.sh` enables
`moeka.service` and disables the legacy `nanobot.service`.
