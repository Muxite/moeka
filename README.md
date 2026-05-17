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
- `./moeka.sh export` / `import` — portable workspace archives.

---

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

---

## Quick start (new machine, Ubuntu 24.04)

```bash
git clone https://github.com/Muxite/moeka.git && cd moeka
./bootstrap.sh
```

`bootstrap.sh` is idempotent: installs `uv`, builds the venv, seeds
`keys.env`, walks you through workspace mode (import / new identity /
onboard), Telegram pairing, and systemd autostart.

## Moving an instance to a new machine

```bash
# On the source machine:
./moeka.sh export                       # -> moeka-export-<host>-<ts>.tar.gz
# Copy the archive over, then on the target:
git clone https://github.com/Muxite/moeka.git && cd moeka
./bootstrap.sh                          # pick [i]mport, point at archive
./moeka.sh telegram-pair                # if bot tokens changed
```

## Spinning up a *different* moeka

```bash
./moeka.sh new alice                    # scaffolds ~/.moeka-alice
export MOEKA_WORKSPACE=~/.moeka-alice
./moeka.sh telegram-pair
./moeka.sh start
```

## Manual quick start (no bootstrap)

```bash
git clone https://github.com/Muxite/moeka.git && cd moeka
cp keys.env.example keys.env
$EDITOR keys.env          # add API keys, bot tokens
./moeka.sh install
./moeka.sh exec onboard   # or ./moeka.sh new NAME
./moeka.sh start
```

To start automatically on boot:

```bash
./moeka.sh enable         # installs systemd user unit + enables linger
```

---

## Commands

| Command | Description |
|---------|-------------|
| `./moeka.sh start` | Start gateway in background |
| `./moeka.sh stop` | Stop gracefully (SIGKILL fallback) |
| `./moeka.sh restart` | Stop then start |
| `./moeka.sh status` | Show process, port, channels, uptime |
| `./moeka.sh logs [-f] [-n N]` | Tail log (follow / line count) |
| `./moeka.sh install` | Create `.venv` and install deps via uv |
| `./moeka.sh doctor` | Health check: runtime, config, keys, service |
| `./moeka.sh shell` | Drop into activated venv |
| `./moeka.sh exec <cmd>` | Run a nanobot subcommand |
| `./moeka.sh version` | Show Python and moeka version |
| `./moeka.sh enable` | Install systemd unit + enable boot autostart |
| `./moeka.sh disable` | Stop service and remove unit |
| `./moeka.sh export [--out FILE] [--with-sessions] [--with-media] [--anonymize]` | Bundle workspace into a portable archive |
| `./moeka.sh import FILE [--force]` | Extract a workspace archive into `$MOEKA_WORKSPACE` |
| `./moeka.sh new NAME [--workspace PATH]` | Scaffold a fresh-identity workspace |
| `./moeka.sh telegram-pair` | Pair a Telegram bot — saves token, captures user ID from first message |

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

Generated by `./moeka.sh exec onboard` on first run, or create manually. Key sections:

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
    "discord":  { "enable": true, "token": "${DISCORD_TOKEN}",  "allowFrom": ["your-user-id"] },
    "canvas":   { "enable": false }
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

Run `./moeka.sh exec onboard` for an interactive setup wizard.

---

## Enabling channels

Atomic flip without hand-editing JSON:

```bash
./moeka.sh exec channels enable telegram
./moeka.sh exec channels disable slack
./moeka.sh exec channels status
```

See the [Features](#features) section above for the full channel matrix.

---

## Directory layout

```
moeka/                    ← this repo
├── moeka.sh              ← universal entrypoint
├── moeka.service         ← systemd user unit
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
./moeka.sh exec onboard   # select "vec" extras, or:
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
MOEKA_WORKSPACE=~/.nanobot-dev ./moeka.sh start
```

Or pass `--workspace ~/.nanobot-dev` to any command.

---

## Credits

Moeka is a fork of [nanobot](https://github.com/HKUDS/nanobot) by HKUDS. Core agent runtime, providers, and channel integrations are built on their work.
