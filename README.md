# Moeka

**Moeka** is a packaged, containerized, systemd-ready personal agent built on top of [nanobot](https://github.com/HKUDS/nanobot). Where vanilla nanobot is a Python library you install and configure by hand, Moeka is an opinionated deployment:

- one script to run it (`./moeka.sh`)
- one file for secrets (`keys.env`), one for non-secret paths (`.env`)
- one systemd unit (`moeka.service`) for start-on-boot
- Docker **and** direct-host modes share the same config, workspace, memory, and skills
- workspace lives at its own standardized path (`MOEKA_WORKSPACE`) so you can `git init` it as a portable agent identity, separate from the instance state
- all mutable state is bind-mounted on the host, so `docker compose down` destroys the containers but keeps memory, history, config, and workspace intact
- inside Docker, shell commands transparently break out to the host so `lsblk`, `docker ps`, `systemctl` — everything — still works

Everything else — the agent loop, channels, providers, tools, skills, MCP support — comes from upstream nanobot and stays pluggable.

---

## What makes Moeka different from nanobot

| Area | Upstream nanobot | Moeka |
|---|---|---|
| Entrypoint | `nanobot gateway …` (remember your flags) | `./moeka.sh start` (single verb, any mode) |
| Secrets | Plaintext in `~/.nanobot/config.json` | Env vars in `keys.env`, resolved into `${VAR}` placeholders |
| State directory | Hardcoded `~/.nanobot` | `MOEKA_STATE` env var, default `~/.nanobot` — many agents on one box |
| Workspace | Nested inside `~/.nanobot/workspace` | `MOEKA_WORKSPACE` env var, default `~/moeka-workspace` — standalone git repo |
| Host access from Docker | None | `MOEKA_EXEC_ON_HOST=1` routes `exec()` through `nsenter` into PID 1's namespaces |
| Docker networking | Bridge + port forwards | `network_mode: host` + `pid: host` — sees the LAN & host processes directly |
| Boot / lifecycle | Manual `pip install`, ad-hoc scripts | `./moeka.sh install` + `bash install-service.sh` = done |
| Version control | Workspace and config intermixed with secrets | Config + workspace are safe to commit; secrets stay in gitignored `keys.env` |

Under the hood, Moeka is just nanobot with a few surgical additions to `nanobot/config/loader.py`, `nanobot/config/paths.py`, and `nanobot/agent/tools/shell.py`. Remove the wrapper files and it's still a valid nanobot checkout.

---

## Quick start

```sh
# 1. Fill in your secrets (never commit this file)
cp keys.env.example keys.env
$EDITOR keys.env

# 2. Install — creates ./.venv in direct mode, or builds the image in docker mode
./moeka.sh install

# 3. Run
./moeka.sh start

# 4. Auto-start on boot (optional)
bash install-service.sh
```

That's it. `./moeka.sh doctor` will tell you what mode it picked, whether `config.json` and `keys.env` are in place, and what Python/Docker it sees.

---

## The four pieces

### 1. `moeka.sh` — universal entrypoint

Auto-detects the right mode for this host:

- **direct mode** (default) — creates `.venv`, `pip install -e .`, `exec nanobot gateway`
- **docker mode** — `docker compose up -d` (triggered by a `.dockerized` flag in the repo, `--docker`, or `MOEKA_MODE=docker`)

```sh
./moeka.sh start           # bring it up
./moeka.sh status          # mode, state dir, running PID
./moeka.sh logs -f         # tail
./moeka.sh restart
./moeka.sh stop
./moeka.sh shell           # drop into the venv / container
./moeka.sh exec -- …       # run any nanobot subcommand
./moeka.sh doctor          # sanity check
```

Flags (may appear before the command): `--docker`, `--direct`, `--config PATH`, `--state PATH`.

### 2. `keys.env` — one file for every secret

`keys.env` is sourced by `moeka.sh` (direct mode) and loaded via `env_file:` (docker mode). Any `${VAR}` placeholder in `config.json` is then resolved at startup by nanobot's existing env-interpolation.

```
keys.env   (gitignored)
  │  sourced by moeka.sh / env_file
  ▼
process env
  │  read by nanobot config loader
  ▼
config.json   (tracked — holds "${OPENROUTER_API_KEY}" etc.)
  │  resolve_config_env_vars()
  ▼
live Config
```

See `keys.env.example` for the full list of supported variables.

### 3. `MOEKA_STATE` and `MOEKA_WORKSPACE` — split state from workspace

Moeka separates two concerns that upstream nanobot conflates:

| Var | Default | Contents | Version-control story |
|---|---|---|---|
| `MOEKA_STATE` | `~/.nanobot` | `config.json`, history, media, cron, per-host caches | Commit `config.json` only; the rest is host-specific runtime data |
| `MOEKA_WORKSPACE` | `~/moeka-workspace` | agent identity docs, skills, memory, notes, scratch files | Initialize as its own git repo and carry between machines |

Both paths flow through `${VAR}` placeholders in `config.json`, so the config is portable across hosts. Multi-agent is just two env vars:

```sh
MOEKA_STATE=~/agents/alice/state MOEKA_WORKSPACE=~/agents/alice/workspace ./moeka.sh start
MOEKA_STATE=~/agents/bob/state   MOEKA_WORKSPACE=~/agents/bob/workspace   ./moeka.sh start
```

The sibling `.env` file (copied from `.env.example`) is the right place to pin these paths for a given host.

### 4. Host bridge — Docker that feels like the host

When running in Docker, `docker-compose.yml` sets `MOEKA_EXEC_ON_HOST=1`. Moeka's shell tool sees that and prefixes every command with `nsenter -t 1 -m -u -n -i -p --` before handing it to `bash -l -c`. Combined with `pid: host`, `network_mode: host`, and a tight set of capabilities (`SYS_ADMIN`, `SYS_PTRACE`, `SYS_CHROOT`, file-capped onto `/usr/bin/nsenter` so the non-root `nanobot` user can use them), the agent sees:

- the host's processes (`ps`, `systemctl --user`, `docker ps`)
- the host's block devices (`lsblk`, `/dev/*`)
- the host's network (LAN services, localhost bindings)

This is a deliberate trade-off: Docker here provides reproducible packaging, **not** a security boundary. If you want strict isolation, leave `MOEKA_EXEC_ON_HOST` unset and drop the three caps from `docker-compose.yml`.

---

## Directory layout

```
.
├── moeka.sh              # universal entrypoint — what you actually run
├── moeka.service         # systemd user unit (calls moeka.sh)
├── install-service.sh    # enable moeka.service, disable legacy nanobot.service
├── restart-nanobot.sh    # legacy-named restart helper (safe from inside the agent)
│
├── keys.env.example      # every supported secret, with comments
├── keys.env              # real secrets — gitignored
├── .env.example          # non-secret runtime paths (MOEKA_STATE, MOEKA_WORKSPACE)
├── .env                  # per-host copy of the above — gitignored
│
├── Dockerfile            # python:3.12-slim + uv + node bridge + util-linux + jq
├── docker-compose.yml    # gateway (host net+pid) and API services
├── entrypoint.sh         # container PID 1
│
├── OPERATIONS.md         # day-to-day guide
├── SYSTEMD.md            # boot service details
│
├── nanobot/              # upstream source (with Moeka's surgical edits)
├── bridge/               # WhatsApp bridge (Node)
├── tests/                # pytest suite
└── docs/                 # deeper-dive technical docs
```

Runtime state (outside this repo):

```
$MOEKA_STATE/            # default ~/.nanobot — per-host runtime
├── config.json          # tracked placeholders like "${OPENROUTER_API_KEY}"
├── history/             # per-channel conversation log
├── media/                # attachments, exports
└── cron/                # scheduled tasks

$MOEKA_WORKSPACE/        # default ~/moeka-workspace — standalone git repo
├── AGENTS.md            # agent identity + behavior contracts
├── SOUL.md              # personality/voice
├── HEARTBEAT.md         # scheduled self-reflection prompts
├── TOOLS.md             # tool usage notes for the agent
├── skills/              # user-authored skills
├── memory/              # vector memory store
├── sessions/            # per-conversation state
└── cron/                # workspace-scoped job records
```

Both paths are bind-mounted into the container at `/home/nanobot/.nanobot` and `/home/nanobot/workspace` respectively, so `docker compose down` tears down containers but every file survives on the host.

---

## Further reading

- [`OPERATIONS.md`](./OPERATIONS.md) — first-time setup, day-to-day commands, multi-agent patterns, host-bridge details
- [`SYSTEMD.md`](./SYSTEMD.md) — how the user service is wired
- [`docs/PYTHON_SDK.md`](./docs/PYTHON_SDK.md) — using nanobot from Python
- [`docs/CHANNEL_PLUGIN_GUIDE.md`](./docs/CHANNEL_PLUGIN_GUIDE.md) — writing a new channel
- [`docs/MEMORY.md`](./docs/MEMORY.md) — how the Dream memory pipeline works
- [`docs/MY_TOOL.md`](./docs/MY_TOOL.md) — the agent's self-modification tool
- [`docs/WEBSOCKET.md`](./docs/WEBSOCKET.md) — WebSocket channel protocol

---

## Credits

Moeka is a deployment layer on top of **nanobot** by HKUDS. All of the core agent
design — the Dream memory pipeline, the Lua-style skill system, the channel
plugin architecture, the provider abstractions, MCP support — is their work.
See [upstream nanobot](https://github.com/HKUDS/nanobot) for design discussion,
release notes, and community.

License: MIT (see [`LICENSE`](./LICENSE)), inherited from upstream.
