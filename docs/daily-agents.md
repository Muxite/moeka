# Daily Moeka instances

Moeka is a general assistant for conversation, research, coding and recurring work.
The Python CLI is `moeka`; `nanobot` remains compatible. From a source checkout,
`bin/moeka.sh` manages running instances. Each command accepts `--workspace PATH`.

```sh
bin/moeka.sh new wonny
bin/moeka.sh list --json
bin/moeka.sh --workspace ~/.moeka-wonny enable
bin/moeka.sh --workspace ~/.moeka-wonny stop
bin/moeka.sh --workspace ~/.moeka-wonny update
moeka agent --workspace ~/.moeka-wonny
```

`update` restarts only the selected instance with the installed core. Review core
patches and acceptance checks before installing a new core. RSI's pinned core is
updated separately; installing a skill does not replace core code. Custom folders
have their own generated systemd user unit; installation is an owner operation.
CLI clients load the selected folder's `.env` and `keys.env` without executing them.

Each folder owns configuration, credentials, sessions, memory, tool state, schedules,
logs, usage, peer queues and promotion history. `.nanobot/workspace-id` is the stable
instance UUID: moving the folder preserves it, opening a copy while the source
exists allocates a new UUID. New instances receive fresh credentials and ports.
Never share a Telegram token: a second poller is refused while other channels work.

## Moving legacy sessions

Back up the workspace and stop its gateway before migrating sibling session storage:

```sh
bin/moeka.sh --workspace ~/.nanobot stop
bin/moeka.sh --workspace ~/.nanobot migrate-sessions
bin/moeka.sh --workspace ~/.nanobot start
```

Migration copies `<root>-sessions` into `<root>/sessions`, uses SQLite's backup API,
checks database integrity and records, and retains originals for rollback. Startup
never moves a live session database. Re-running a completed migration refuses
existing destinations. Keep the old folder and backup until verification finishes.

## Personality and permissions

`personality.name` controls the displayed persona; `agents.defaults.botName` controls
client labels. `SOUL.md` holds tone only. Fixed operating rules are bundled separately
and remain in every prompt. Existing personalities are preserved during installation.

The main Moeka has `personality.allowSelfEdit=false`. Wonny has it enabled: the
`personality` tool can stage up to 150 words, list hashed versions, and restore a
version. Revisions apply when the instance next becomes idle between turns. Dream
cannot rewrite `SOUL.md`. File tools and command sandboxes protect owner policy.

Daily agents use `tools.exec.allowSudo=false`, workspace restriction, Bubblewrap on
Linux, and services/direct command children use NoNewPrivileges. Sandboxes hide
configuration, credentials, sessions, peer state, promotion state and policy files;
only isolated workspace work is writable. Script tools requiring the configured
sandbox fail closed if it is unavailable. Never grant Docker sockets, host writable
binds or provider secrets to command children. On Linux hosts restricting user
namespaces, the owner must permit `/usr/bin/bwrap` through a narrow AppArmor profile
(the machine setup uses `profile moeka-bwrap /usr/bin/bwrap flags=(unconfined) { userns, }`).
Other platforms can use the existing Seatbelt backend or an owner-managed sandbox.

## Model choice

Enable `modelRouting` with `{ "enabled": true, "mode": "auto" }` for the daily pair.
Routing is opt-in, so SDKs and existing configured providers keep their behavior.
Auto starts a task with Luna; `model_select` can choose GPU for a later model call
without rerunning tools. Explicit user selections take precedence in that task.
Both presets are available to worker spawning and model commands.

The dedicated service uses `gpt-oss:20b` at `http://127.0.0.1:11437/v1`. Start the
owner-managed service with `docker compose -f compose.inference.yaml up -d`, then
`docker exec moeka-ollama ollama pull gpt-oss:20b`. Its isolated volume and network
are named `moeka-ollama` and `moeka-inference`; provision them once with `docker
volume create moeka-ollama` and `docker network create moeka-inference`. Do not
point it at another project's inference service. This host uses an 16384-token
context and 2048 output-token GPU preset, within its 16 GB card's practical limits.

Readiness is bounded to two seconds and initial inference output to 30 seconds
(configurable). A fresh explicit GPU request retries even after an earlier failure.
Overload, unavailable service, GPU exhaustion or timeout falls back to Luna with a
brief reason. Cold model loading can exceed the deadline; the service keeps the
loaded model for 24 hours. Metering records requested preset, actual provider/model,
fallback reason and instance UUID. A shared host meter is optional.

RSI can share this server with `compose.moeka-shared.yml` in its own repository;
its meter joins the dedicated inference network. GPU-lock and evaluator changes
are excluded. The daily assistants work without RSI or that meter.

## Daily work

The `memory` tool remembers, searches lexically, corrects and forgets facts with
session/message sources. It remains usable without embeddings. Durable transcripts
and Dream consolidation continue independently per instance. Six non-overwriting
skill seeds cover test running, Python, structured data, notes, documents and media;
the model discovers applicable skills on demand. Seeds declare effects, dependency
checks, bounded output and structured failures. Optional processors require their
stated local dependencies.

Requested reminders and task results use durable cron delivery. Unsolicited
heartbeat notifications default to one per day, between 09:00 and 21:00 in the
configured timezone. Telegram keeps short paragraphs, typing indicators and ordered
message bursts; routine progress/tool hints can be disabled independently.

See [peer messaging](guides/paired-agents.md) for owner-paired, authenticated local/HTTPS
work requests, and [certified transfers](capability-transfer.md) for destination
previews, approval, idle activation, trust/revocation checks, history and rollback.
Personality, memory, conversations, credentials and schedules never travel with a
capability transfer. Neither daily agent accepts sudo effects.
