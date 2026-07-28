# moeka

A personal AI agent for server management — homelabs, Docker stacks, and Linux administration. Moeka runs natively on your host using [`uv`](https://docs.astral.sh/uv/), with no containers required. Reach it over Telegram, Discord, Slack, Matrix, a web UI, or any of the other built-in chat channels from anywhere.

Built on [nanobot](https://github.com/HKUDS/nanobot) by HKUDS, with a CS/server-management focus: a permissive sandbox tuned for legitimate homelab ops (`rm`, `dd`, `mkfs`, `format`, `shutdown` are *not* blocked by default), cross-process session locking, dispatcher auto-restart, semantic vector memory, and a `nanobot channels enable/disable` CLI.

---

## Features

### Agent runtime
- **Model presets + automatic fallback** — declare `agents.defaults.model_preset` and `fallbackModels`; the gateway hot-swaps providers per turn without a restart.
- **Streaming reasoning** — thinking/CoT chunks render as a separate channel above the assistant bubble (per-channel `showReasoning` toggle).
- **Sustained goals (`/goal`)** — multi-step missions tracked across turns via `long_task` / `complete_goal`.
- **Subagents** — `spawn` a focused worker with its own tool registry; results re-injected via system inbound.
- **Dream two-phase memory consolidation** — automatic summarization with `/dream`, `/dream_log`, `/dream_restore`.
- **Cross-process FileLock on session save** — safe even if a stray second moeka process starts alongside the systemd one.
- **Dispatcher watchdog** — outbound queue auto-restarts on unexpected crashes.

### Chat channels
| Channel | Highlights |
|---|---|
| **Telegram** | Inline keyboard buttons, streaming edits, `drop_pending_updates` default-true, full slash command palette (`/goal`, `/pairing`, `/model`, …). |
| **Discord** | Thread-aware sessions, DM pairing, media uploads. |
| **Slack** | Socket-mode with handshake timeout; block-kit action buttons; pairing-only mode when `allowFrom` is omitted. |
| **WebSocket / WebUI** | Built-in WebUI behind the gateway: token-bootstrapped, signed media URLs, runtime model badge, transcript persistence, goal state replay on reconnect. |
| **Feishu / Lark** | Topic-thread isolation, CardKit streaming, Lark global domain, p2p pairing. |
| **WhatsApp** | Voice transcription via Groq/OpenAI Whisper, media bridge. |
| **MS Teams** | Hardened auth check; stale-reference cleanup. |
| **Matrix** | E2E encryption via `matrix-nio`. |
| **Email** | Attachment support, self-loop guard. |
| **QQ / WeChat / WeCom / DingTalk / MoChat** | Group chat, voice, QR/media. |

Pairing: omit `allowFrom` for any channel and unapproved DMs receive a chat-native pairing code instead of being silently dropped. Approve with `/pairing approve <code>`.

### Providers
OpenRouter, Anthropic, OpenAI, OpenAI Responses, OpenAI Codex (OAuth), Azure OpenAI, AWS Bedrock (native Converse), GitHub Copilot (OAuth), DeepSeek (incl. V4/Reasoner thinking), Kimi (K2.5/K2.6 thinking), Xiaomi MiMo (thinking), Moonshot, MiniMax, Mistral, Qwen, Gemini, Groq, Hugging Face, NVIDIA NIM, LM Studio, Ollama, vLLM, VolcEngine + Coding Plan, BytePlus, LongCat, StepFun, Cohere, Together, Olostep, Brave, Kagi, custom OpenAI-compatible. Transcription via Groq or OpenAI Whisper (now honours per-provider `api_base`).

### Tools
Filesystem (read/write/edit/list, hash-deduped reads), `exec` shell, web search/fetch (BYO-key for Brave/Kagi/Olostep/HuggingFace), `cron` scheduler, `notebook_edit` for `.ipynb`, MCP servers (stdio + SSE + streamable-HTTP with TCP probe), background subagents (`spawn`), runtime self-inspection (`MyTool`), image generation, `long_task` / `complete_goal` for sustained goals.

### Sandbox (server-management posture)
- **Internal guards (non-tunable)** — direct writes to `history.jsonl` / `.dream_cursor` are blocked because they corrupt Dream's cursor.
- **Default user-tunable deny** — only the fork-bomb pattern. Moeka deliberately allows `rm -rf`, `dd`, `mkfs`, `format`, `shutdown`, `>/dev/sd*` because these are legitimate homelab ops. Tighten with `tools.exec.deny_patterns` if you want upstream's stricter posture back.
- **`allow_sudo` opt-in** — `sudo` is rejected by default; flip `tools.exec.allow_sudo = true` to permit.
- **SSRF guard** — internal/private URLs blocked in both `exec` shell output URLs and `web_fetch`.
- **`restrict_to_workspace`** — optional confinement of filesystem & exec to the workspace root.

### Operations
- `nanobot gateway` — single process, all channels.
- `nanobot channels enable <name>` / `disable <name>` — atomic config flip without hand-editing JSON.
- `nanobot channels status` / `login` — discoverable channel state and OAuth/QR flows.
- OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- Cron with chat-native natural-language scheduling.
- `./bin/moeka.sh export` / `import` — portable workspace archives.

### Embeddable core (moeka-core)
- **`from nanobot.core import MoekaCore`** — drop the agent/RAG engine into your own Python with no channels, gateway, or WebUI. The import boundary is enforced by a test, so it pulls no chat-runtime dependencies.
- **Data, not files** — build from an in-memory `config_dict` / `Config` (or a `config_path`); `${VAR}` placeholders resolve from the environment (the same `keys.env` pattern). With no workspace it runs in a throwaway temp dir instead of touching `~/.nanobot`.
- **Host actions + documents** — register plain Python functions as tools with `@core.action`, ingest docs for retrieval, and run a multi-step tool-calling loop. One-shot `complete()` / `acomplete()` skip the loop entirely.
- Minimal install: `pip install moeka[core]` (add `[vec]` for semantic RAG). See [Embedding the core](#embedding-the-core-moeka-core) below.

---

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

---

## Quick start (new machine, Ubuntu 24.04)

```bash
git clone https://github.com/Muxite/moeka.git && cd moeka
./bin/bootstrap.sh
```

`bootstrap.sh` is idempotent: installs `uv`, builds the venv, seeds
`keys.env`, walks you through workspace mode (import / new identity /
onboard), Telegram pairing, and systemd autostart.

## Moving an instance to a new machine

```bash
# On the source machine:
./bin/moeka.sh export                       # -> moeka-export-<host>-<ts>.tar.gz
# Copy the archive over, then on the target:
git clone https://github.com/Muxite/moeka.git && cd moeka
./bin/bootstrap.sh                          # pick [i]mport, point at archive
./bin/moeka.sh telegram-pair                # if bot tokens changed
```

## Spinning up a *different* moeka

```bash
./bin/moeka.sh new alice                    # scaffolds ~/.moeka-alice
export MOEKA_WORKSPACE=~/.moeka-alice
./bin/moeka.sh telegram-pair
./bin/moeka.sh start
```

## Manual quick start (no bootstrap)

```bash
git clone https://github.com/Muxite/moeka.git && cd moeka
cp keys.env.example keys.env
$EDITOR keys.env          # add API keys, bot tokens
./bin/moeka.sh install
./bin/moeka.sh exec onboard   # or ./bin/moeka.sh new NAME
./bin/moeka.sh start
```

To start automatically on boot:

```bash
./bin/moeka.sh enable         # installs systemd user unit + enables linger
```

---

## Commands

| Command | Description |
|---------|-------------|
| `./bin/moeka.sh start` | Start gateway in background |
| `./bin/moeka.sh stop` | Stop gracefully (SIGKILL fallback) |
| `./bin/moeka.sh restart` | Stop then start |
| `./bin/moeka.sh status` | Show process, port, channels, uptime |
| `./bin/moeka.sh logs [-f] [-n N]` | Tail log (follow / line count) |
| `./bin/moeka.sh install` | Create `.venv` and install deps via uv |
| `./bin/moeka.sh doctor` | Health check: runtime, config, keys, service |
| `./bin/moeka.sh shell` | Drop into activated venv |
| `./bin/moeka.sh exec <cmd>` | Run a nanobot subcommand |
| `./bin/moeka.sh version` | Show Python and moeka version |
| `./bin/moeka.sh enable` | Install systemd unit + enable boot autostart |
| `./bin/moeka.sh disable` | Stop service and remove unit |
| `./bin/moeka.sh export [--out FILE] [--with-sessions] [--with-media] [--anonymize]` | Bundle workspace into a portable archive |
| `./bin/moeka.sh import FILE [--force]` | Extract a workspace archive into `$MOEKA_WORKSPACE` |
| `./bin/moeka.sh new NAME [--workspace PATH]` | Scaffold a fresh-identity workspace |
| `./bin/moeka.sh telegram-pair` | Pair a Telegram bot — saves token, captures user ID from first message |

Flags accepted by most commands:

```
--config PATH      path to config.json (default: $MOEKA_WORKSPACE/config.json)
--workspace PATH   override workspace directory (default: ~/.nanobot)
```

---

## Configuration

Moeka loads configuration in three layers:

### 1. `keys.env` — secrets (never commit)

```bash
OPENROUTER_API_KEY=sk-or-...
TELEGRAM_TOKEN=123456:ABC...
DISCORD_TOKEN=...
```

Copy `keys.env.example` to get started. Variables defined here are injected into the environment before the agent starts; use `${VAR}` placeholders in `config.json` to reference them.

### 2. `.env` — non-secret settings

```bash
MOEKA_WORKSPACE=~/.nanobot   # agent state directory
NANOBOT_CONFIG=~/.nanobot/config.json
```

### 3. `config.json` — agent configuration

Generated by `./bin/moeka.sh exec onboard` on first run, or create manually. Key sections:

```jsonc
{
  "agents": {
    "defaults": {
      "model": "openai/gpt-4o-mini",   // provider/model string
      "dreamInterval": 7200            // memory consolidation interval (seconds)
    }
  },
  "channels": {
    "telegram": { "enable": true, "token": "${TELEGRAM_TOKEN}", "allowFrom": ["your-user-id"] },
    "discord":  { "enable": true, "token": "${DISCORD_TOKEN}",  "allowFrom": ["your-user-id"] }
  },
  "tools": {
    "exec": {
      "enable": true,
      "allow_sudo": false   // set true to allow sudo commands
    }
  },
  "gateway": {
    "host": "127.0.0.1",
    "port": 8900
  }
}
```

Run `./bin/moeka.sh exec onboard` for an interactive setup wizard.

---

## Enabling channels

Atomic flip without hand-editing JSON:

```bash
./bin/moeka.sh exec channels enable telegram
./bin/moeka.sh exec channels disable slack
./bin/moeka.sh exec channels status
```

See the [Features](#features) section above for the full channel matrix.

---

## Directory layout

```
moeka/                    ← this repo
├── bin/                  ← user entrypoints
│   ├── moeka.sh          ← universal launcher
│   └── bootstrap.sh      ← first-run setup
├── scripts/              ← ops & helpers (install-service, moeka.service, …)
├── docs/                 ← documentation
├── keys.env.example      ← secrets template
├── .env.example          ← non-secret env template
├── pyproject.toml        ← Python package (uv manages deps)
└── nanobot/              ← agent source

~/.nanobot/               ← MOEKA_WORKSPACE (agent state)
├── config.json           ← live configuration
├── SOUL.md               ← agent personality
├── USER.md               ← user profile
├── AGENTS.md             ← agent instructions
├── memory/MEMORY.md      ← long-term memory
├── skills/               ← custom skills
├── sessions/             ← per-channel conversation state
└── moeka.log             ← runtime log
```

---

## Permissions & sandbox

Moeka's sandbox is tuned for legitimate server-management work. By default:

- `sudo` is **denied** — set `tools.exec.allow_sudo = true` in `config.json` to permit, and ensure your host's sudoers policy permits it.
- Destructive system commands (`rm -rf`, `dd`, `mkfs`, `format`, `shutdown`, raw block-device writes) are **allowed** — moeka is a homelab agent. To re-add upstream's stricter defaults, set `tools.exec.deny_patterns` in config.
- Writes to `history.jsonl` / `.dream_cursor` are **always blocked** (these are non-tunable internal-state guards).
- `restrict_to_workspace = true` confines filesystem & exec to `MOEKA_WORKSPACE`.

---

## Semantic memory

Enable vector search over memory, history, and skills:

```bash
./bin/moeka.sh exec onboard   # select "vec" extras, or:
uv pip install -e ".[vec]" --project .
```

Then add to `config.json`:
```json
"vec": { "enable": true, "embeddingModel": "all-MiniLM-L6-v2", "topK": 10 }
```

---

## Sustained goals

```text
/goal       Tell the agent to treat the next request as a long-running mission.
            Inspect, plan, then call long_task; complete_goal recaps when done.
/history    Print the last N persisted messages for the current session.
/dream      Force memory consolidation now.
/pairing    list | approve <code> | deny <code> | revoke <user_id>
/model      Show or switch the active model preset (hot-reload — no restart).
/status     Runtime, provider, channel status.
```

---

## Running multiple agents

Set a different workspace per agent:

```bash
MOEKA_WORKSPACE=~/.nanobot-dev ./bin/moeka.sh start
```

Or pass `--workspace ~/.nanobot-dev` to any command.

---

## Embedding the core (moeka-core)

`nanobot.core.MoekaCore` is the reusable agentic/RAG "thinking core" — embed the
engine in your own Python without channels, gateway, or WebUI (the import has no
chat-runtime dependencies, and a guard test keeps it that way). It needs **data,
not files**: a config file is just one way to produce the pydantic `Config` it
consumes.

```python
from nanobot.core import MoekaCore

# No ~/.nanobot needed — pass config as a dict; ${VAR} resolves from the
# environment (the same keys.env pattern), and with no workspace given the core
# runs in a throwaway temp dir instead of touching ~/.nanobot.
core = MoekaCore.create(config_dict={
    "providers": {"openrouter": {"apiKey": "${OPENROUTER_API_KEY}"}},
    "agents": {"defaults": {"model": "google/gemini-3-flash-preview",
                            "provider": "openrouter"}},
})

@core.action
def get_disk_usage(path: str) -> str:
    "Return human-readable disk usage for a path."
    import shutil
    total, used, free = shutil.disk_usage(path)
    return f"{used // 2**30} GiB used, {free // 2**30} GiB free"

result = await core.run("How much disk is free on /?")
print(result.content, result.tools_used)
core.cleanup()  # remove the ephemeral workspace
```

`MoekaCore.create()` accepts **at most one** config source: `config=` (a built
`Config`), `config_dict=` (a dict), or `config_path=` (a file); with none it
discovers `~/.nanobot/config.json` like the bot does. `MoekaCore.from_config(config)`
is the pure `(Config, workspace) → core` data seam. The one-shot
`complete()` / `acomplete()` helpers take the same `config=` / `config_dict=` /
`config_path=` inputs, so neither the loop nor a single completion ever requires a
file on disk. Install the minimal dependency surface with the `core` extra
(`pip install moeka[core]`); add `[vec]` for semantic RAG.

---

## Credits

Moeka is a fork of [nanobot](https://github.com/HKUDS/nanobot) by HKUDS. Core agent runtime, providers, and channel integrations are built on their work.
