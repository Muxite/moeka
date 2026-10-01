# Multiple Instances

moeka is not a singleton. Any number of gateway instances, one-shot agents,
containers and embedded `moeka.Kernel`s can run on one machine. This page states the
rules that keep them apart (spec `specs/005-multi-instance/`).

## Instances

- An instance is identified by its **root**: the workspace directory, which in the
  flat layout also holds `config.json` and the instance's state.
- **Default instance**: `~/.nanobot` (unit `moeka.service`).
- **Named instance**: `~/.moeka-<name>`, `<name>` matching `^[a-z0-9][a-z0-9_-]{0,31}$`
  (unit `moeka@<name>.service`).
- **Registered instance**: any other root created with `moeka.sh new NAME --workspace PATH`;
  its absolute path is appended to `~/.config/moeka/instances` (one path per line). It
  has no systemd unit.
- **Discovered instances**: the default one when `~/.nanobot/config.json` exists, every
  `~/.moeka-*/config.json`, and every registry line whose `config.json` exists.
  Discovery is a host operation (the `moeka.sh` script, the gateway, the legacy floor
  adapter); kernel code never discovers.
- Sessions live next to the root, in `<root>-sessions/<id>/sessions.db`.

## The control script

Every `bin/moeka.sh` command acts on exactly one instance, chosen before any env
file is loaded: `--workspace PATH`, else `MOEKA_WORKSPACE`, else `~/.nanobot`. An env
file that sets `MOEKA_WORKSPACE` cannot move it (a warning is printed). A root that
still contains `${` exits 2 and creates nothing.

```bash
./bin/moeka.sh new work                     # ~/.moeka-work, own ports + Unix socket
./bin/moeka.sh new lab --ws-tcp             # TCP WebSocket port instead of a socket
./bin/moeka.sh new box --workspace /srv/box # registered instance at a custom path
./bin/moeka.sh --workspace ~/.moeka-work start
./bin/moeka.sh --workspace ~/.moeka-work status --json
./bin/moeka.sh list --json                  # every discovered instance
./bin/moeka.sh --workspace ~/.moeka-work stop   # stops this instance only
```

- `stop` stops the instance's own unit when it is active, and the process in
  `<root>/moeka.pid` only when `/proc/<pid>/cmdline` names the instance's config. Any
  other PID file is stale: nothing is signalled and the file is removed. SIGKILL
  follows after `MOEKA_STOP_TIMEOUT_S` seconds (default 10). No process is ever matched
  by name.
- `status` exits 0 when the instance runs (its unit is active, its PID file names its
  gateway, or `<root>/gateway.lock` is held) and 3 when it does not; `--json` prints
  `name`, `workspace`, `config`, `running`, `pid`, `manager`, `unit`, `ports`
  (`gateway`, `api`, `websocket`) and `websocket_socket`.
- `run` (the unit's `ExecStart`) writes `<root>/moeka.pid`, holds `<root>/gateway.lock`
  for its lifetime and execs `nanobot gateway --config <root>/config.json --workspace <root>`.
  A duplicate `run` exits 0 and starts nothing.
- Env files load in this order, later values winning: `<repo>/.env` and
  `<repo>/keys.env` (only for the default instance, or with `MOEKA_REPO_ENV=1`), then
  `<root>/.env`, then `<root>/keys.env`. `telegram-pair` writes to `<root>/keys.env`
  (mode 0600).
- Test and automation hooks: `MOEKA_NANOBOT_BIN` (the gateway binary; no venv is
  created), `MOEKA_SYSTEMCTL`, `MOEKA_LOGINCTL`, `MOEKA_RUN_DIR`.

## Ports and sockets

`moeka.sh new` picks a port base `B` so that no two instances collide:

- `gateway.port = B`, `api.port = B + 1`; the WebSocket channel listens on the Unix
  socket `<root>/run/websocket.sock` (directory mode 0700), or on `127.0.0.1:B+2` with
  `--ws-tcp`.
- Without `--port-base`, `B` is the smallest of `18800, 18810, ..., 19790` whose ports
  are used by no discovered instance (schema defaults 18790, 8900 and 8765 count for a
  config that omits them) and are bindable on `127.0.0.1` right now.
- `--port-base N` must be in `[1024, 65533]` (exit 2); a port already assigned to an
  instance or bound by another process exits 1 and names it.
- Port selection and config writing are serialised by a lock in the run dir
  (`$MOEKA_RUN_DIR`, else `$XDG_RUNTIME_DIR/moeka`, else `/tmp/moeka-<uid>`), so
  concurrent `new` runs never share ports.
- A socket path longer than 107 bytes exits 2 (`unix socket path too long`); use
  `--ws-tcp` for such roots.

## systemd units

```bash
scripts/install-service.sh work             # renders moeka@.service, enables moeka@work
scripts/install-service.sh work --dry-run   # prints the unit and the commands only
./bin/moeka.sh --workspace ~/.moeka-work enable    # the same
./bin/moeka.sh --workspace ~/.moeka-work disable   # systemctl --user disable --now moeka@work
```

`scripts/moeka@.service` is a template: `%i` is the instance name, the root is
`%h/.moeka-%i`, secrets come from `%h/.moeka-%i/keys.env`. Installing a named unit never
touches `moeka.service` or `nanobot.service`, never runs `sudo`, and only prints the
`loginctl enable-linger` command when lingering is off.

## One writer per state dir

- A writer holds an exclusive `flock` on `<state_dir>/.instance.lock` and records itself
  in `<state_dir>/.instance.json` (`pid`, `hostname`, `started_at`, `argv0`, `mode`).
  Writers are `moeka.Kernel(env)` (`attach="write"`, the default), `nanobot gateway`,
  `nanobot serve` and the one-shot / `--classic` `nanobot agent`. In the flat layout the
  state dir is the workspace.
- A second writer is refused at once: `Kernel(...)` raises
  `moeka.errors.InstanceLockedError` (`state_dir`, `holder`, `reason`), and the CLI
  writers exit 3 with the holder on stderr. A refused writer writes nothing. The lock
  is released by `close()`/`aclose()` or by process exit, SIGKILL included. A
  filesystem without `flock` refuses writers (`reason="lock_unsupported"`).
- `Kernel(env, attach="read_only")` takes no lock and creates nothing under the state
  dir: sessions can be listed and read, usage queried, existing memory stores searched
  and `kernel.llm` used; every mutating call raises `ReadOnlyKernelError`.
- Read-only commands (`nanobot status`, `nanobot sessions`, `nanobot provider`,
  `moeka.sh status/list/doctor/logs`) never take the lock.
- The interactive `nanobot agent` (native TUI) talks to the running gateway and is not a
  writer.

## One poller per bot token

- Before a Telegram or Discord channel starts, the channel manager locks
  `<run dir>/channel-locks/<channel>-<16 hex of sha256(token)>.lock` and writes a JSON
  sidecar (`pid`, `workspace`, `config`, `started_at`); neither file holds the token.
- A second instance with the same token reports the channel as `state: "locked"`
  (`channel_token_in_use: ...`) and starts every other channel normally.
- Instances that do not share a run dir (two containers) cannot see each other's lock.
  Telegram's `Conflict` error then pauses polling (`polling_state: "conflict"`), logs one
  ERROR line per episode and retries once after `channels.telegram.conflictRetryS`
  seconds (default 60, 5 to 3600).

## One budget and one ledger across processes

- Every process on one data dir writes the same usage store
  (`<data_dir>/llm_usage.sqlite3`); `kernel.usage.total()` counts all of them and
  `kernel.usage.total(consumer=c)` exactly consumer `c`'s calls. Writes that fail on a
  locked database are retried on the next write and at close (up to 5 seconds), so a
  normal close loses nothing.
- `moeka.budget.CapBudget` is in-memory and per process. `moeka.budget.SharedCapBudget(
  data_dir, budget_id, limit_usd=..., ...)` keeps caps, spend and reservations in the
  usage store: N processes or containers on one host share one cap. Admission is one
  SQLite write transaction; a lock it cannot get within `lock_timeout_s` refuses the
  call (`BudgetExceeded.reason_code == "budget_unavailable"`). A reservation whose
  lease expires (its holder died) is charged in full and reported as `budget.expire`.
- The data dir must be on a local filesystem; containers sharing it must run on the
  same host (one clock).

## Many Kernels and agent cores

- **One Kernel per state dir** (writers): a second writer Kernel on the same state dir,
  in this process or another, is refused.
- **Many Kernels may share a data dir**: `Environment.for_host(..., data_dir=D)` puts the
  usage store at `D/llm_usage.sqlite3` for every Kernel built with it; under
  `strict=True` `D` must not be (inside) the `work_dir`.
- **Distinct `memory_key` per agent**: agents in one Kernel keep their memory under
  `<state_dir>/agents/<memory_key>/memory` and their default session `agent:<memory_key>`.
  Two live agents with the same effective key share both; the kernel warns and emits a
  `kernel.memory_key_shared` trace event naming both agents.

## Remaining per-process globals

Code that holds a `Config` uses that config's own directory. These process-wide values
remain, and only legacy (config-file) hosts depend on them; Kernel-native hosts
(`moeka.Kernel` built from `Environment.for_host`) depend on none of them:

- The **current config path** (`nanobot.config.loader.set_config_path` /
  `get_config_path`): the default for code without a config (`get_data_dir()`,
  `get_media_dir()`, `get_logs_dir()`, the CLI history default). `nanobot gateway` pins it
  with `pin_config_path`; switching a pinned process to another config raises
  `ConfigPathConflictError`, so one process runs one gateway config.
- The **state home** (`get_state_home()`: `MOEKA_WORKSPACE`, then `~/.nanobot`): the
  default instance identity of `nanobot gateway` and the fallback workspace.
- The **run dir** (`MOEKA_RUN_DIR` / `XDG_RUNTIME_DIR`): where token and `new` locks live.

## Isolation between instances (file tools only)

The file tools' protected floor denies, for read and write, every path whose last
component is `keys.env`, the `.env` directly inside an instance root, and every other
instance's root and `<root>-sessions` sibling; it write-protects the own instance's
`.instance.lock`, `.instance.json`, `moeka.pid`, `gateway.lock` and `run/`.

This covers the **file tools only**. The `exec` tool can still reach all of these paths:
instances that share a UID are not isolated from each other's shell commands. Real
separation between instances needs separate UIDs, the bwrap sandbox
(`tools.exec.sandbox: "bwrap"`), or containers (see [deployment.md](deployment.md)).
