# Moeka operations guide

Moeka runs natively on the host via a Python venv managed by UV.

## First-time setup

```sh
# 1. Copy secret template and fill in real keys
cp keys.env.example keys.env
$EDITOR keys.env          # DO NOT commit keys.env

# 2. Install dependencies (creates ./.venv)
./moeka.sh install

# 3. Verify
./moeka.sh doctor
```

## Day-to-day

```sh
./moeka.sh start          # run the gateway
./moeka.sh status         # workspace, config, running PID
./moeka.sh logs -f        # tail output
./moeka.sh restart
./moeka.sh stop
./moeka.sh shell          # drop into the venv
./moeka.sh exec -- <cmd>  # run a nanobot subcommand
```

## Boot setup

```sh
./moeka.sh enable         # install + enable systemd user service
./moeka.sh disable        # stop + disable service

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
| `./moeka.sh`                  | Universal entrypoint                                          |
| `./moeka.service`             | systemd user unit                                             |

## Multiple agents

Each agent is a single directory. Name two paths and you get two agents:

```sh
MOEKA_WORKSPACE=~/agents/alice ./moeka.sh start
MOEKA_WORKSPACE=~/agents/bob   ./moeka.sh start   # different terminal
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
2. **Sudoers**: passwordless sudo for the user

```sh
./moeka.sh setup-sudo          # interactive
./moeka.sh setup-sudo --yes    # non-interactive (writes /etc/sudoers.d/moeka-sudo)
```

When enabled, moeka cannot run sudo commands blindly. The exec tool detects
`sudo` and returns a `SUDO_REQUIRED` prompt — moeka must re-call with
`SUDO_JUSTIFIED:<reasoning> | <command>`, articulating why the action is
safe. The justification is logged at WARNING level before execution.

Check status with `./moeka.sh doctor`.

## systemd

See [SYSTEMD.md](./SYSTEMD.md). `./moeka.sh enable` installs and starts
the service. `./moeka.sh disable` stops and disables it.
