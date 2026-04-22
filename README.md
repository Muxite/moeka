# Moeka

**Moeka** is a packaged, containerized, systemd-ready personal agent built on top of [nanobot](https://github.com/HKUDS/nanobot). Where vanilla nanobot is a Python library you install and configure by hand, Moeka is an opinionated deployment:

- one script to run it (`./moeka.sh`)
- one file for secrets (`keys.env`)
- one systemd unit (`moeka.service`) for start-on-boot
- Docker **and** direct-host modes share the same config, workspace, memory, and skills
- inside Docker, shell commands transparently break out to the host so `lsblk`, `docker ps`, `systemctl` — everything — still works

Everything else — the agent loop, channels, providers, tools, skills, MCP support — comes from upstream nanobot and stays pluggable.

---

## What makes Moeka different from nanobot

| Area | Upstream nanobot | Moeka |
|---|---|---|
| Entrypoint | `nanobot gateway …` (remember your flags) | `./moeka.sh start` (single verb, any mode) |
| Secrets | Plaintext in `~/.nanobot/config.json` | Env vars in `keys.env`, resolved into `${VAR}` placeholders |
| State directory | Hardcoded `~/.nanobot` | `MOEKA_STATE` env var, default `~/.nanobot` — many agents on one box |
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

### 3. `MOEKA_STATE` — many agents on one box

Every state path derives from `MOEKA_STATE` (default `~/.nanobot`). Change that, and you get a fresh agent with its own config, workspace, history, and memory:

```sh
MOEKA_STATE=~/agents/alice ./moeka.sh start
MOEKA_STATE=~/agents/bob   ./moeka.sh start    # in another terminal
```

Each state directory can itself be a git repo — check in `config.json` + `workspace/` + `skills/`, gitignore `keys.env` + `history/` + `media/`. You can now deploy a roster of agents by cloning repos.

### 4. Host bridge — Docker that feels like the host

When running in Docker, `docker-compose.yml` sets `MOEKA_EXEC_ON_HOST=1`. Moeka's shell tool sees that and prefixes every command with `nsenter -t 1 -m -u -n -i -p --` before handing it to `bash -l -c`. Combined with `pid: host`, `network_mode: host`, and `CAP_SYS_ADMIN`, the agent sees:

- the host's processes (`ps`, `systemctl --user`, `docker ps`)
- the host's block devices (`lsblk`, `/dev/*`)
- the host's network (LAN services, localhost bindings)

This is a deliberate trade-off: Docker here provides reproducible packaging, **not** a security boundary. If you want strict isolation, leave `MOEKA_EXEC_ON_HOST` unset.

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
│
├── Dockerfile            # python:3.12-slim + uv + node bridge + util-linux
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

State (outside the repo, at `$MOEKA_STATE` — default `~/.nanobot`):

```
~/.nanobot/
├── config.json           # tracked placeholders like "${OPENROUTER_API_KEY}"
├── workspace/            # agent's projects, notes, skills
├── history/              # per-channel conversation log
├── media/                # attachments, exports
└── cron/                 # scheduled tasks
```

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
