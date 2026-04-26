# Moeka

**Moeka** is a nanobot for server management — a native agent built on [nanobot](https://github.com/HKUDS/nanobot) for managing homelabs, Docker infrastructure, and Linux servers. It runs directly on the host via UV and a Python venv, so it has natural access to everything it manages.

- one script to run it (`./moeka.sh`)
- one file for secrets (`keys.env`), one for non-secret paths (`.env`)
- one command for boot setup (`./moeka.sh enable`)
- one directory for the whole agent — `MOEKA_WORKSPACE` (default `~/.nanobot`) holds config, identity, skills, memory, sessions, media, everything
- configurable sudo (disabled by default, opt-in through config and host policy)

Everything else — the agent loop, channels, providers, tools, skills, MCP support — comes from upstream nanobot and stays pluggable.

---

## Quick start

```sh
# 1. Fill in your secrets (never commit this file)
cp keys.env.example keys.env
$EDITOR keys.env

# 2. Install — creates .venv with all dependencies
./moeka.sh install

# 3. Run
./moeka.sh start

# 4. Auto-start on boot (optional)
./moeka.sh enable
```

`./moeka.sh doctor` will tell you whether Python, UV, config, keys, and systemd are in place.

---

## Commands

```sh
./moeka.sh start           # run the nanobot gateway
./moeka.sh stop            # stop the running instance
./moeka.sh restart         # stop + start
./moeka.sh status          # workspace, config, running PID
./moeka.sh logs -f         # tail output (via journalctl)
./moeka.sh shell           # drop into the venv
./moeka.sh exec -- ...     # run any nanobot subcommand
./moeka.sh install         # create .venv and install deps
./moeka.sh doctor          # sanity check everything
./moeka.sh enable          # install + enable systemd user service
./moeka.sh disable         # stop + disable systemd user service
```

Flags: `--config PATH`, `--workspace PATH`.

---

## How it works

### `keys.env` — one file for every secret

`keys.env` is sourced by `moeka.sh`. Any `${VAR}` placeholder in `config.json` is resolved at startup.

```
keys.env   (gitignored)
  |  sourced by moeka.sh
  v
process env
  |  read by nanobot config loader
  v
config.json   (tracked — holds "${OPENROUTER_API_KEY}" etc.)
  |  resolve_config_env_vars()
  v
live Config
```

See `keys.env.example` for the full list of supported variables.

### `MOEKA_WORKSPACE` — the one-directory instance

Default `~/.nanobot`. Holds config, identity docs, skills, memory, sessions, media, cron, history — everything. `git init` it to carry an agent between machines.

Multi-agent is one env var:

```sh
MOEKA_WORKSPACE=~/agents/alice ./moeka.sh start
MOEKA_WORKSPACE=~/agents/bob   ./moeka.sh start   # different terminal
```

### Two permission tiers

| Tier | Exec | Sudo | How to enable |
|---|---|---|---|
| **Non-sudo** (default) | Runs as current user | No | Default |
| **Sudo** (opt-in) | Runs as current user + sudo | Yes, through normal exec guards | `tools.exec.allowSudo: true` plus host sudo policy |

**Sudo opt-in:** Requires `tools.exec.allowSudo: true` in `config.json` and a host sudo policy that lets the Moeka process run the intended elevated commands. When disabled, the exec tool blocks commands containing `sudo` with one clear error. When enabled, sudo commands run through the same safety guards as any other command.

---

## Directory layout

```
.
├── moeka.sh              # universal entrypoint
├── moeka.service         # systemd user unit
├── install-service.sh    # enable moeka.service, disable legacy nanobot.service
├── restart-nanobot.sh    # restart helper (safe from inside the agent)
│
├── keys.env.example      # every supported secret, with comments
├── keys.env              # real secrets — gitignored
├── .env.example          # non-secret runtime paths (MOEKA_WORKSPACE)
├── .env                  # per-host copy — gitignored
│
├── pyproject.toml        # Python package (installed via uv)
├── OPERATIONS.md         # day-to-day guide
├── SYSTEMD.md            # boot service details
│
├── nanobot/              # upstream source (with moeka's surgical edits)
├── bridge/               # WhatsApp bridge (Node)
├── tests/                # pytest suite
└── docs/                 # deeper-dive technical docs
```

The instance directory (outside this repo):

```
$MOEKA_WORKSPACE/           # default ~/.nanobot
├── config.json             # "${OPENROUTER_API_KEY}" etc.
├── SOUL.md                 # personality / voice
├── AGENTS.md               # agent identity + behavior
├── HEARTBEAT.md            # periodic tasks
├── TOOLS.md                # tool usage notes
├── USER.md                 # user-authored context
├── skills/                 # user-authored skills
├── memory/                 # vector memory + dream history
├── sessions/               # per-channel conversation state
├── media/                  # attachments, exports
├── cron/                   # scheduled job registry
├── history/                # CLI + shared history
└── tool-results/           # persisted overflow
```

---

## Further reading

- [OPERATIONS.md](./OPERATIONS.md) — first-time setup, day-to-day commands, multi-agent patterns
- [SYSTEMD.md](./SYSTEMD.md) — how the user service is wired
- [docs/PYTHON_SDK.md](./docs/PYTHON_SDK.md) — using nanobot from Python
- [docs/CHANNEL_PLUGIN_GUIDE.md](./docs/CHANNEL_PLUGIN_GUIDE.md) — writing a new channel
- [docs/MEMORY.md](./docs/MEMORY.md) — how the Dream memory pipeline works
- [docs/MY_TOOL.md](./docs/MY_TOOL.md) — the agent's self-modification tool
- [docs/WEBSOCKET.md](./docs/WEBSOCKET.md) — WebSocket channel protocol

---

## Credits

Moeka is a deployment layer on top of **nanobot** by HKUDS. All of the core agent design — the Dream memory pipeline, the Lua-style skill system, the channel plugin architecture, the provider abstractions, MCP support — is their work. See [upstream nanobot](https://github.com/HKUDS/nanobot) for design discussion, release notes, and community.

License: MIT (see [LICENSE](./LICENSE)), inherited from upstream.
