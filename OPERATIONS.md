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

In addition to exec, the host filesystem is bind-mounted at matching paths
(`/home:/home`, `/etc:/etc`, `/var:/var`, `/opt:/opt`, `/tmp:/tmp`, etc.) so
that file tools (`read_file`, `write_file`, `glob`, `grep`) access host files
directly — no path translation needed. The container user (uid 1000) maps to
the host user, so file permissions behave identically to a native install.

## Permissions: non-sudo and sudo

Moeka has two permission tiers. Both work identically in direct and docker mode.

### Non-sudo (always on)

No restrictions beyond what the host user (uid 1000) can do:

- File tools: unrestricted (`restrict_to_workspace: false`, no `allowed_dir`)
- Exec: runs on the host via nsenter bridge, as the host user
- Network: host network mode, full LAN visibility

This is the same capability as running `nanobot gateway` natively.

### Sudo (opt-in)

Two things must be in place:

1. **Config**: `tools.exec.allowSudo: true` in `config.json`
2. **Sudoers**: passwordless sudo for the host user

```sh
# One-time host setup:
./moeka.sh setup-sudo          # interactive
./moeka.sh setup-sudo --yes    # non-interactive (writes /etc/sudoers.d/moeka-sudo)
```

When enabled, moeka cannot run sudo commands blindly. The exec tool detects
`sudo` in commands and returns a `SUDO_REQUIRED` prompt — moeka must re-call
with `SUDO_JUSTIFIED:<reasoning> | <command>`, articulating why the action is
safe. The justification is logged at WARNING level before execution.

Check status with `./moeka.sh doctor`:

```
sudo rule     : installed (/etc/sudoers.d/moeka-sudo)
allow_sudo    : enabled in config
```

## systemd

See [SYSTEMD.md](./SYSTEMD.md). `bash install-service.sh` enables
`moeka.service` and disables the legacy `nanobot.service`.
