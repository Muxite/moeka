# Feature Specification: Multi-Instance moeka (no singleton)

**Feature Branch**: `005-multi-instance`
**Created**: 2026-10-01
**Status**: Draft
**Input**: Owner ruling 2026-10-01: "moeka must not be a singleton: any number of moeka instances, processes,
containers, agents and agent cores on one machine." Master plan section M3 (multi-instance, containers, flexible
agent cores) and the multi-instance findings in the findings register (stop kills every instance, single unit,
`new` reuses ports, missing `MOEKA_WORKSPACE` falls back to `~/.nanobot`, two pollers on one bot token conflict,
processes sharing a `state_dir` lose writes, `CapBudget` lives in memory only).

**Verification pattern**: held-out spec tests (master plan, owner 2026-10-01). A tester agent writes hidden tests
from this file only; an implementer agent writes code from this file only. Both agree on surfaces through the
[Test Harness Contract](#test-harness-contract) section. Feedback to the implementer is per requirement id only.

## Terms

- **Instance**: one moeka gateway deployment, identified by its **instance root** (the workspace directory, which in
  the flat layout is also the state dir and the directory holding `config.json`).
- **Default instance**: the instance whose root is `$HOME/.nanobot` (name `default`).
- **Named instance**: an instance whose root is `$HOME/.moeka-<name>`; `<name>` matches `^[a-z0-9][a-z0-9_-]{0,31}$`.
- **Registered instance**: an instance created by `moeka.sh new NAME --workspace PATH` at a custom path, recorded in
  the **instance registry** file `$HOME/.config/moeka/instances` (one absolute path per line).
- **Discovered instances**: the default instance (if `$HOME/.nanobot/config.json` exists), every
  `$HOME/.moeka-*/config.json`, and every registry line whose `config.json` exists. Discovery is a host-side
  operation (CLI, gateway, legacy adapter); kernel code never discovers.
- **Sessions sibling**: `<root>-sessions`, the directory next to an instance root that holds `sessions.db`.
- **Kernel**: a `moeka.Kernel`; its **state dir** is `env.paths.state_dir`, its **data dir** is
  `env.paths.data_dir` (where the usage store `llm_usage.sqlite3` lives).
- **Writer**: a process component that mutates a state dir (a gateway, a one-shot CLI agent, `nanobot serve`, a
  Kernel opened with `attach="write"`).
- **Run dir**: the per-user directory for cross-instance locks: `$MOEKA_RUN_DIR` if set, else
  `$XDG_RUNTIME_DIR/moeka` if `XDG_RUNTIME_DIR` is set, else `/tmp/moeka-<uid>`; created with mode 0700.

## User Scenarios & Testing *(mandatory)*

Consumers: the owner running several bots on one box, an embedding host running many agent cores, the RSI harness
running N worker processes against one budget, and container deployments.

### User Story 1 - Several gateway instances side by side (Priority: P1)

The owner creates and runs any number of instances from one checkout. Each gets its own ports, socket, state and
secrets; starting, stopping or inspecting one never touches another.

**Why this priority**: today `moeka.sh stop` kills every gateway on the machine and `new` reuses ports, so a second
instance is unsafe; every other story builds on instances being separable.

**Independent Test**: with a temporary `HOME`, a fake `nanobot` binary and a fake `systemctl`, create three
instances with `moeka.sh new`, start all three, stop one, and check the other two are still running and that all
ports and sockets are distinct.

**Acceptance Scenarios**:

1. **Given** instances `a` and `b` started with `moeka.sh start`, **When** `moeka.sh --workspace ~/.moeka-a stop`
   runs, **Then** `a`'s process exits and `b`'s process (same PID) is still alive.
2. **Given** `moeka.sh new a` then `moeka.sh new b` without flags, **When** their configs are read, **Then** their
   gateway and API ports are pairwise distinct, distinct from every other discovered instance, and their WebSocket
   channels listen on Unix sockets inside their own roots.
3. **Given** `MOEKA_WORKSPACE='${MOEKA_WORKSPACE}'` (unexpanded), **When** any `moeka.sh` command or
   `nanobot gateway` runs, **Then** it exits with code 2 and creates no directory.

---

### User Story 2 - One writer per state, one poller per bot token (Priority: P1)

A second process that would write the same state dir is refused with a clear error, or attaches read-only when it
asks to. Two instances configured with the same Telegram or Discord token never both poll it.

**Why this priority**: concurrent writers silently lose writes (memory files, session caches, cron state), and two
Telegram pollers loop on `Conflict` errors forever.

**Independent Test**: open a writer Kernel on a state dir, then try a second writer in the same process and in a
child process; both are refused with `InstanceLockedError` naming the holder. Start two channel managers with one
Telegram token; the second reports the token as in use without any network call.

**Acceptance Scenarios**:

1. **Given** a running gateway on root R, **When** a second `nanobot gateway` (or one-shot `nanobot agent -m`)
   starts on R, **Then** it exits with code 3 and its stderr names R and the holder PID.
2. **Given** a writer Kernel on state dir S, **When** `Kernel(env_on_S, attach="read_only")` opens, **Then** it can
   list sessions and query usage, and any mutating call raises `ReadOnlyKernelError` without writing.
3. **Given** a Telegram channel receiving `telegram.error.Conflict`, **When** the error is handled, **Then** the
   channel stops polling, reports state `conflict`, and logs one ERROR line for the episode.

---

### User Story 3 - One budget and one usage ledger across processes and containers (Priority: P1)

A spend cap set for a job holds no matter how many processes or containers share it; usage totals from all of them
add up exactly.

**Why this priority**: `CapBudget` is in-memory per process, so N workers can each spend the whole cap; the RSI
harness and awork need caps that hold across workers (owner rulings on paid models depend on it).

**Independent Test**: four processes on one data dir, each admitting calls against one `SharedCapBudget` with a USD
cap, under a fake provider; the committed spend plus open reservations never exceeds the cap, and the usage store
holds exactly the calls made.

**Acceptance Scenarios**:

1. **Given** a `SharedCapBudget(data_dir=D, budget_id="job", limit_usd=0.50)` in four processes, **When** each tries
   50 calls whose worst case is $0.01 and whose settled cost equals the worst case, **Then** exactly 50 calls are
   admitted in total and 150 are refused with `BudgetExceeded` code `cap_usd`.
2. **Given** a process holding an open reservation is killed with SIGKILL, **When** the lease expires, **Then** any
   other process's next admission or snapshot counts that reservation as spent and no longer as reserved.

---

### User Story 4 - Many agent cores (Priority: P2)

An embedding host runs several Kernels in one process (one per state dir) and several processes on one shared data
dir; each agent in a Kernel has its own memory and default session when given its own `memory_key`.

**Why this priority**: the flexible-agent-core goal; the rules exist implicitly today and must be stated and
proven.

**Independent Test**: two Kernels in one process on different state dirs sharing one data dir, each running calls
under its own consumer; usage per consumer matches the calls each made; two agents with different `memory_key`s
write to different memory directories.

**Acceptance Scenarios**:

1. **Given** `Environment.for_host(..., data_dir=D)` for two state dirs, **When** both Kernels make calls, **Then**
   both write to `D/llm_usage.sqlite3` and `kernel.usage.total(consumer=c)` equals each one's own calls.
2. **Given** two `AgentSpec`s in one Kernel with the same effective memory key, **When** the second agent is built,
   **Then** a `kernel.memory_key_shared` trace event names both agents.

---

### User Story 5 - Per-instance systemd units (Priority: P2)

Each named instance runs under its own user unit `moeka@<name>.service`, installed with
`scripts/install-service.sh <name>`; the existing `moeka.service` is never modified by this.

**Why this priority**: boot autostart for more than one instance; the live unit must stay untouched (owner).

**Independent Test**: with a temporary `HOME` and a fake `systemctl`, run `install-service.sh a`; the rendered
template unit is written, `moeka.service` is not written, and the recorded systemctl calls name only `moeka@a`.

**Acceptance Scenarios**:

1. **Given** `~/.moeka-a/config.json` exists, **When** `install-service.sh a` runs, **Then**
   `~/.config/systemd/user/moeka@.service` exists with the repo path substituted, and `systemctl --user enable
   --now moeka@a.service` was called.
2. **Given** an existing `~/.config/systemd/user/moeka.service`, **When** `install-service.sh a` runs, **Then**
   that file's bytes are unchanged and no systemctl call names `moeka.service` or `nanobot.service`.

---

### User Story 6 - Containers (Priority: P2)

The owner runs instances in containers next to host instances: a minimal image and compose file, UID 1000, state on
`/data`, authenticated wildcard binds, and a host Ollama reachable from inside.

**Why this priority**: containers are the real isolation layer for shell execution (constitution II limit) and the
portable way to run many instances.

**Independent Test**: build the image, start two compose projects with different host ports plus one host
instance; all three health endpoints answer and their state lives in three different places.

**Acceptance Scenarios**:

1. **Given** the image, **When** `docker run --rm --entrypoint id <image> -u` runs, **Then** it prints `1000`.
2. **Given** `MOEKA_TOKEN_ISSUE_SECRET` unset, **When** the container starts, **Then** it exits with code 2 before
   binding any port.

---

### User Story 7 - Instances cannot read each other through the file tools (Priority: P2)

An agent in one instance cannot read or write another instance's root, sessions, locks or any `keys.env` through
the file tools.

**Why this priority**: several instances on one box share a UID; secrets and sessions of one must not be one
`read_file` away from another's agent.

**Independent Test**: build the file-tool floor for instance `a` with instances `b` and `default` present; every
path in a fixed matrix of protected paths is denied for read and write, every control path is allowed.

**Acceptance Scenarios**:

1. **Given** instance `a` and an existing `~/.moeka-b`, **When** a's file tool reads `~/.moeka-b/SOUL.md`, **Then**
   it is denied with the protected-path marker.
2. **Given** any path whose last component is `keys.env`, **When** read or written through the file tools, **Then**
   it is denied.

---

### User Story 8 - No hidden process-wide instance (Priority: P3)

Code that has a config uses that config's own directory, not a process-wide "current config" or a literal
`~/.nanobot`; a second gateway config in the same process is refused.

**Why this priority**: residual singletons that make an instance's files land in another instance's directory.

**Independent Test**: bind the process config path to A, build a legacy environment from config B, and check B's
data, media and logs dirs are under B's directory.

**Acceptance Scenarios**:

1. **Given** `set_config_path(A)` and a config loaded from `B/config.json`, **When** `LegacyEnvironment.from_config`
   builds paths, **Then** `paths.data_dir == B`.
2. **Given** `pin_config_path(A)`, **When** `set_config_path(B)` is called, **Then** it raises
   `ConfigPathConflictError`.

---

### Edge Cases

Instances and ports:
- `moeka.sh new NAME` when `~/.moeka-NAME` exists and is non-empty: exit 1, nothing written.
- `NAME` not matching the name pattern (uppercase, `/`, 33+ chars, leading `-`): exit 2.
- `--port-base` below 1024, above 65533, or non-numeric: exit 2.
- `--port-base N` where N or N+1 (or N+2 with `--ws-tcp`) is already assigned to a discovered instance: exit 1, the
  message names the port and that instance's root.
- A port free in every config but bound by an unrelated process: auto-selection skips it; explicit `--port-base`
  exits 1.
- No free port base in the auto range: exit 1 with message `no free port base`.
- The Unix socket path would exceed 107 bytes: exit 2 with message containing `unix socket path too long` and the
  hint `--ws-tcp`.
- Two `moeka.sh new` runs at the same time: they must not receive the same ports (selection is serialised by a lock
  in the run dir).

Stop/status:
- PID file points at a live process that is not this instance's gateway (PID reuse): not signalled; reported as a
  stale PID file; the file is removed.
- PID file contains garbage or is empty: treated as stale.
- Gateway ignores SIGTERM: SIGKILL after `MOEKA_STOP_TIMEOUT_S` (default 10) seconds.
- Instance managed by its systemd unit and also a stray PID-file process: both stopped; nothing else.
- `status` on a never-started instance: exit 3, `running: false`.
- The host has no `procps` (`ps`, `pgrep`): commands still work (they read `/proc`).

Locks:
- Holder killed with SIGKILL: the OS releases the lock; the next writer acquires within 2 seconds.
- State dir on a filesystem without `flock` support: the writer refuses to start (`InstanceLockedError` with reason
  `lock_unsupported`) rather than run unlocked.
- Same process opens a second writer Kernel on the same state dir (also via a symlinked path that resolves to it):
  refused.
- A read-only attach while no writer runs: allowed; a writer may start later while it stays attached.
- A read-only attach to a state dir that does not exist: `ReadOnlyKernelError`, no directory created.
- Channel token is empty or still an unexpanded `${VAR}`: no token lock is taken (the channel fails as today).
- Same token used by a Telegram channel in two containers that do not share a run dir: the local lock cannot see it;
  the `Conflict` handling covers it.
- Telegram `Conflict` arrives in webhook mode: logged once; no polling state change.

Budget:
- A reservation's lease expires while its call is still running in a live process: the reservation is charged as
  spend; the late settle and release true it up (FR-046).
- The usage database is write-locked longer than `lock_timeout_s`: admission refuses (fail closed) with code
  `budget_unavailable`; no provider call is sent.
- Two processes construct the same `budget_id` with different caps: the second raises `ValueError` unless it passes
  `reset_caps=True`.
- An unpriced model under a USD cap: refused with `unpriced_model` unless `allow_unpriced=True` (same as
  `CapBudget`).
- The data dir is on a network filesystem: unsupported; documented, not detected.
- Clock skew between containers on one host: none assumed (one kernel clock); different hosts are out of scope.

Containers:
- The `/data` volume is owned by another UID: the container exits non-zero with a message naming `/data` and UID 1000.
- `MOEKA_OLLAMA_API_BASE` unset: defaults to `http://host.docker.internal:11434/v1`.
- `tools.exec.sandbox` is `bwrap` in a container without `CAP_SYS_ADMIN`/user namespaces: documented limit; the
  exec tool fails as it does on the host without namespaces.

## Requirements *(mandatory)*

Every requirement names its proof in brackets: `[T]` held-out test, `[S]` static check of a file, `[D]` doc check,
`[M]` measurement in an SC run.

### A. Per-instance control script (`bin/moeka.sh`)

- **FR-001**: The instance a `moeka.sh` invocation acts on MUST be determined before any env file is loaded, from
  `--workspace PATH`, else `MOEKA_WORKSPACE`, else `$HOME/.nanobot`, with a leading `~` expanded. An env file that
  sets `MOEKA_WORKSPACE` to a different value MUST NOT change it (a warning is printed). [T]
- **FR-002**: If the instance root value contains the substring `${` after `~` expansion, every `moeka.sh` command
  MUST exit with code 2, print an error containing `MOEKA_WORKSPACE`, and create no file or directory. [T]
- **FR-003**: `moeka.sh` MUST load env files in this order, later values winning: `<repo>/.env` and
  `<repo>/keys.env` (only when Q3's rule allows), then `<root>/.env`, then `<root>/keys.env`. [T]
- **FR-004**: `moeka.sh stop` MUST act only on the selected instance: (a) if the instance's own unit (FR-012) is
  active, stop that unit; (b) if `<root>/moeka.pid` names a live process whose `/proc/<pid>/cmdline` contains the
  instance's absolute config path, send SIGTERM, wait up to `MOEKA_STOP_TIMEOUT_S` seconds (default 10), then
  SIGKILL; (c) otherwise treat the PID file as stale, signal nothing and delete it. It MUST NOT signal any other
  process and MUST NOT use pattern-based process matching (`pkill -f`, `pgrep -f`, `killall`). Exit code 0 in all
  three cases. [T][S]
- **FR-005**: `moeka.sh status` MUST report only the selected instance and exit 0 when it runs, 3 when it does not.
  "Runs" means the instance's unit is active, or its PID file names a live process matching FR-004(b), or its
  `<root>/gateway.lock` is held. [T]
- **FR-006**: `moeka.sh status --json` MUST print exactly one JSON object on stdout with keys `name`, `workspace`,
  `config`, `running` (bool), `pid` (int or null), `manager` (`"systemd"`, `"pid"` or null), `unit` (string or
  null), `ports` (object with integer-or-null `gateway`, `api`, `websocket`), and `websocket_socket` (absolute path
  or null). [T]
- **FR-007**: `moeka.sh doctor` and `moeka.sh logs` MUST use the same instance detection as FR-005 (no
  `pgrep -f`); `logs` reads the journal of the instance's own unit when active, else `<root>/moeka.log`. [T][S]
- **FR-008**: `moeka.sh run` MUST write its own PID to `<root>/moeka.pid`, hold an exclusive `flock` on
  `<root>/gateway.lock` for the process lifetime, and `exec` the gateway with both `--config <root>/config.json`
  (or `MOEKA_CONFIG`) and `--workspace <root>`. When the lock is already held it MUST exit 0 with a warning and
  start nothing. [T]
- **FR-009**: `moeka.sh start` MUST treat "already running" per instance: when the instance's own unit is active or
  FR-005 says it runs, print a warning and exit 0 without starting a second process. [T]
- **FR-010**: `moeka.sh telegram-pair` MUST write the token and user id to `<root>/keys.env` (created with mode 0600
  if missing), never to `<repo>/keys.env`. [T]
- **FR-011**: `moeka.sh list --json` MUST print a JSON array with one object per discovered instance, each with the
  keys of FR-006; `moeka.sh list` prints one line per instance. [T]
- **FR-012**: The instance's own unit MUST be `moeka.service` for the default instance, `moeka@<name>.service` for a
  named instance, and none for a registered instance at a custom path. `enable`, `disable`, `start`, `stop`,
  `restart`, `status` and `logs` MUST address only that unit. [T]

### B. Creating instances and assigning ports (`moeka.sh new`)

- **FR-013**: `moeka.sh new NAME [--workspace PATH] [--port-base N] [--ws-tcp]` MUST validate `NAME` against
  `^[a-z0-9][a-z0-9_-]{0,31}$` (else exit 2) and refuse a non-empty target (exit 1). Default target is
  `$HOME/.moeka-NAME`. With `--workspace PATH` the absolute path is appended to the instance registry. [T]
- **FR-014**: The written `config.json` MUST set `agents.defaults.workspace` to the absolute target path (never a
  `${...}` placeholder). [T]
- **FR-015**: The written config MUST set `gateway.port = B` and `api.port = B + 1` for the chosen port base `B`.
  Without `--ws-tcp` it MUST set `channels.websocket.unixSocketPath = <root>/run/websocket.sock` (absolute) and
  create `<root>/run/` with mode 0700; with `--ws-tcp` it MUST set `channels.websocket.host = "127.0.0.1"` and
  `channels.websocket.port = B + 2` and leave `unixSocketPath` empty. [T]
- **FR-016**: Without `--port-base`, `B` MUST be the smallest value in `18800, 18810, ..., 19790` such that every
  port the instance needs (`B`, `B+1`, and `B+2` with `--ws-tcp`) is (a) not used by any discovered instance's
  `gateway.port`, `api.port` or TCP `channels.websocket.port` (schema defaults 18790, 8900, 8765 apply when a
  config omits them; a WebSocket with `unixSocketPath` uses no TCP port), and (b) bindable on `127.0.0.1` at that
  moment. No such `B`: exit 1 with `no free port base`. [T]
- **FR-017**: With `--port-base N`, `N` MUST be an integer in `[1024, 65533]` (else exit 2); a needed port that is
  used by a discovered instance or not bindable MUST exit 1 naming the port (and the instance root when known). [T]
- **FR-018**: Port selection and config writing of concurrent `new` runs MUST be serialised (a lock file in the run
  dir) so two concurrent runs never get overlapping ports. [T]
- **FR-019**: When `<root>/run/websocket.sock` would exceed 107 bytes and `--ws-tcp` is not given, `new` MUST exit 2
  with a message containing `unix socket path too long` and create nothing. [T]
- **FR-020**: `new` MUST create `<root>/keys.env` from `keys.env.example` (or empty) with mode 0600 and print the
  chosen ports and socket path. [T]

### C. Template systemd unit

- **FR-021**: The repo MUST contain `scripts/moeka@.service`, a systemd user template unit that: sets
  `Environment=MOEKA_WORKSPACE=%h/.moeka-%i`; loads `EnvironmentFile=-%h/.moeka-%i/keys.env`; starts
  `@MOEKA_REPO@/bin/moeka.sh --workspace %h/.moeka-%i run`; has no `WorkingDirectory=` line and no reference to
  `%h/projects/moeka` or to the repo-level `keys.env`/`.env`; keeps `Restart=on-failure` and the backoff settings of
  `moeka.service`. [S]
- **FR-022**: `scripts/install-service.sh <name>` MUST: validate `<name>` (exit 2); require
  `$HOME/.moeka-<name>/config.json` (else exit 1 with a hint to run `moeka.sh new <name>`); write
  `$HOME/.config/systemd/user/moeka@.service` with `@MOEKA_REPO@` replaced by the absolute repo path; run
  `systemctl --user daemon-reload`; and run `systemctl --user enable --now moeka@<name>.service` unless
  `--no-enable` is given (then neither enable nor start). It MUST NOT run `sudo` or `loginctl enable-linger`; when
  linger is off it prints the command for the owner. `--dry-run` prints the rendered unit and the commands it
  would run and changes nothing. [T]
- **FR-023**: `install-service.sh` with a name MUST NOT create, modify, stop, disable or remove `moeka.service` or
  `nanobot.service`, and MUST NOT call systemctl with either name. `install-service.sh` without arguments keeps its
  current behaviour. [T]
- **FR-024**: `moeka.sh --workspace ~/.moeka-<name> enable|disable` MUST call `install-service.sh <name>` and
  `systemctl --user disable --now moeka@<name>.service` respectively; for the default instance they keep today's
  behaviour. [T]

### D. Fail fast on an unexpanded workspace

- **FR-025**: `Config.workspace_path` MUST raise `UnexpandedWorkspaceError` (a `ValueError`, importable from
  `nanobot.config.schema`) when the effective workspace string contains `${`; the message MUST contain the
  offending value and the text `MOEKA_WORKSPACE`. It MUST NOT fall back to the state home. Other missing `${VAR}`
  references keep the current warn-and-continue behaviour. [T]
- **FR-026**: `nanobot gateway`, `nanobot serve` and one-shot `nanobot agent -m` with such a config (and no
  `--workspace` override) MUST exit with code 2, print the error to stderr, and create no directory whose name
  contains `${` and no `$HOME/.nanobot`. A `--workspace` override with a valid path MUST make the same config
  work. [T]

### E. Config-owned paths instead of process globals

- **FR-027**: `LegacyEnvironment.from_config(config)` for a config loaded from file F MUST give
  `paths.data_dir == F.parent`, `paths.media_dir == F.parent/"media"`, `paths.logs_dir == F.parent/"logs"`
  (resolved), whatever the process-global config path is. [T]
- **FR-028**: The gateway runtime's default-instance identity MUST be `get_state_home()/"config.json"` (resolved),
  not a literal `~/.nanobot` path: with `MOEKA_WORKSPACE=X`, `GatewayInstance.resolve(config_path=X/"config.json")`
  MUST give `paths.state_path == X/"run"/"gateway.json"`. [T]
- **FR-029**: `GatewayServiceInstaller` launchd rendering MUST put stdout/stderr logs under
  `<instance data dir>/logs/`, where the instance data dir is the parent of `options.start.config_path` when set,
  else the resolved `options.start.workspace` when set, else `get_state_home()`; never a literal `~/.nanobot`. [T]
- **FR-030**: `get_cli_history_path`, `get_bridge_install_dir` and `get_legacy_sessions_dir` (in
  `nanobot.config.paths`) MUST accept an optional `data_dir` argument and return `data_dir/"history"/"cli_history"`,
  `data_dir/"bridge"` and `data_dir/"sessions"` respectively; with no argument they MUST derive from
  `get_data_dir()` (the active config file's directory), not from `get_state_home()`. Call sites that hold a
  `Config` loaded from a file MUST pass `config.runtime_data_dir`. [T]
- **FR-031**: `nanobot.config.loader.pin_config_path(path)` MUST bind the process config path and pin it; after
  that, `set_config_path(p)` or `pin_config_path(p)` with a different resolved path MUST raise
  `ConfigPathConflictError` (a `RuntimeError`, importable from `nanobot.config.loader`), and the same path is a
  no-op. `reset_config_path()` clears binding and pin (for teardown and tests). `nanobot gateway` MUST pin its
  config path at startup. [T]
- **FR-032**: `docs/multiple-instances.md` MUST list every remaining per-process global that affects paths (at least
  the current config path), state that Kernel-native hosts do not depend on any of them, and state the
  "one Kernel per state dir; many Kernels may share a data dir; distinct `memory_key` per agent" rules. [D]

### F. Per-state-dir instance lock and read-only attach

- **FR-033**: A writer MUST hold an exclusive OS lock (`flock`) on `<state_dir>/.instance.lock` for its lifetime and
  write `<state_dir>/.instance.json` with `pid`, `hostname`, `started_at` (ISO 8601 UTC), `argv0` and `mode`
  after acquiring it. Writers are: `moeka.Kernel` with `attach="write"` (the default), `nanobot gateway`,
  `nanobot serve`, and the one-shot and `--classic` `nanobot agent` paths. For the legacy flat layout the state dir
  is the workspace. [T]
- **FR-034**: When the lock is held (by another process, or by another writer in the same process, including via a
  path that resolves to the same directory), the second writer MUST be refused within 2 seconds:
  `moeka.Kernel(...)` raises `InstanceLockedError` (importable from `moeka.errors`) whose message contains the
  resolved state dir and, when `.instance.json` is readable, `pid <n>`; its attributes `state_dir` (Path) and
  `holder` (dict or None) carry the same data. CLI writers exit with code 3 and print the same message to stderr.
  A refused writer MUST NOT create or modify any file under the state dir or the sessions sibling. [T]
- **FR-035**: The lock MUST be released by `Kernel.close()`/`aclose()` and by process exit (including SIGKILL); a
  new writer MUST then acquire it. [T]
- **FR-036**: If the lock cannot be taken because the filesystem does not support it, the writer MUST be refused
  with `InstanceLockedError` whose `reason == "lock_unsupported"` (CLI exit 3). Every other refusal has
  `reason == "held"`. [T]
- **FR-037**: `moeka.Kernel(env, attach="read_only")` MUST NOT take the lock and MUST NOT create any file or
  directory under the state dir or sessions sibling (SQLite `-wal`/`-shm` sidecars of existing databases excepted).
  Opening read-only on a missing state dir MUST raise `ReadOnlyKernelError` (importable from `moeka.errors`). Any
  other `attach` value raises `ValueError`. [T]
- **FR-038**: On a read-only Kernel these MUST work: `kernel.usage.*` read methods (`total`, `totals`, `records`,
  `budget`, `loss`, `checkpoint`, `verify`), `kernel.sessions.get` and `kernel.sessions.list`, a returned session's
  `messages`, `metadata`, `snapshot()`, and `kernel.memory(scope)` read methods (`search`, `count`, `sources`,
  `get_meta`) on an existing store. `kernel.llm` calls MUST also work (they write only to the data dir). These MUST
  raise `ReadOnlyKernelError` before writing anything: `kernel.agent(...)`, `kernel.sessions.open/create/
  create_sync/restore_sync/delete_sync`, session `append`/`append_sync`/`rewind_sync`/`fork_sync`/
  `set_metadata_sync`, memory `add`/`clear`/`set_meta`, `kernel.memory(scope)` for a store file that does not exist,
  and epistemics writes. [T]
- **FR-039**: Read-only commands (`nanobot status`, `nanobot sessions` listing and export, `nanobot provider`,
  `moeka.sh status/list/doctor/logs`) MUST NOT take the lock and MUST work while a writer runs. [T]
- **FR-040**: The file-tool floor MUST deny writes to `<state_dir>/.instance.lock`, `<state_dir>/.instance.json`,
  `<root>/moeka.pid`, `<root>/gateway.lock` and anything under `<root>/run/`. [T]

### G. Per-token channel lock and Telegram `Conflict`

- **FR-041**: Before a channel with a bot token starts (at least `telegram` and `discord`), the channel manager MUST
  acquire an exclusive lock `<run dir>/channel-locks/<channel>-<h>.lock` where `<h>` is the first 16 hex digits of
  SHA-256 of the token, and write a sidecar `<same>.json` with `pid`, `workspace`, `config`, `started_at`. Neither
  file may contain the token or its full hash. The lock is released when the channel stops or the process exits.
  Empty or unexpanded (`${...}`) tokens take no lock. [T]
- **FR-042**: When that lock is held, the channel MUST NOT be started (no network call), `ChannelManager.get_status()`
  MUST report it with `state == "locked"` and an `error` string starting `channel_token_in_use:` that names the
  holder's workspace and pid (when the sidecar is readable), and every other channel of the instance MUST start
  normally. The public helper `nanobot.channels.token_lock.acquire_channel_token_lock(channel, token, *,
  run_dir=None, owner=None)` returns a releasable lock object (context manager, `.release()`), or raises
  `ChannelTokenInUseError` with attribute `holder` (dict or None). [T]
- **FR-043**: `TelegramChannel.handle_polling_error(exc)` MUST be the polling error callback. For
  `telegram.error.Conflict` it MUST: set `polling_state = "conflict"`; stop issuing `getUpdates`; schedule one
  restart attempt no earlier than `conflict_retry_s` seconds later (config `conflictRetryS`, default 60, allowed
  range 5 to 3600, outside it a validation error); and log exactly one ERROR record per conflict episode containing
  `Conflict` and `another process is polling this bot token` (never the token). A conflict episode ends at the
  first successful `getUpdates` round trip, which sets `polling_state = "polling"`. While in conflict,
  `ChannelManager.get_status()` reports the channel with `state == "conflict"`. Other errors keep today's
  handling. In webhook mode a `Conflict` is logged once and changes no state. [T]

### H. Cross-process budget on the shared usage store

- **FR-044**: `moeka.budget.SharedCapBudget(data_dir, budget_id="default", *, limit_usd=None, limit_tokens=None,
  per_tag=None, allow_unpriced=False, lease_s=3600.0, lock_timeout_s=10.0, reset_caps=False)` MUST implement the
  `Budget` protocol (`admit`, `settle`, `release`) and `snapshot(tag=None, value=None)` with exactly the keys and
  meaning of `CapBudget.snapshot` (`budget-event.v1`), plus read-only properties `spent_usd`, `spent_tokens`,
  `reserved_usd`, `reserved_tokens` and `exposure(tag=None, value=None)`. Its state lives in
  `<data_dir>/llm_usage.sqlite3` and is shared by every `SharedCapBudget` with the same resolved `data_dir` and
  `budget_id`, in any process or container on the host. [T]
- **FR-045**: Admission MUST be one atomic write transaction that: (1) expires reservations whose lease has passed
  (FR-047); (2) computes spent plus open reserved over all holders; (3) refuses exactly as `CapBudget.admit` would
  (same arithmetic, same `BudgetExceeded.reason_code` values `cap_usd`, `cap_tokens`, `cap_tag`, `unpriced_model`,
  same `scope`); (4) otherwise inserts an open reservation of the estimate's worst case with
  `lease_expires = now + lease_s`. Consequence (the cap invariant): at every committed state,
  `spent + reserved <= cap` for every capped scope, provided no settled attempt cost exceeds its reservation. [T]
- **FR-046**: `settle(reservation, event)` and `release(reservation)` MUST follow `CapBudget` semantics exactly
  (settled cost becomes spend and consumes the reservation; an attempt with tokens but no price, or with outcome
  `timeout` or `cancelled`, marks the reservation so its remainder is charged at release, per Q1; release frees
  the rest once; a second release is a no-op). For a reservation already expired by FR-047, settle and release MUST
  true it up so that the final spend equals what it would have been without the expiry. [T]
- **FR-047**: An open reservation whose `lease_expires` has passed MUST be treated as charged: its remaining amount
  counts as spent and no longer as reserved, in every later admission and snapshot from any process. The process
  that marks it expired MUST emit a `budget.expire` trace event with `call_id` and the charged USD and tokens. [T]
- **FR-048**: If the admission transaction cannot obtain the database write lock within `lock_timeout_s`, admission
  MUST raise `BudgetExceeded` with `reason_code == "budget_unavailable"` and no provider call is sent (fail closed).
  A settle or release that cannot be written MUST be retried on the next operation of that budget object and at
  `Kernel.close()`; until written, the reservation stays open (counted), so caps never loosen. [T]
- **FR-049**: The first `SharedCapBudget` for a `budget_id` stores its caps; a later construction with different
  caps (any of `limit_usd`, `limit_tokens`, `per_tag`, `allow_unpriced`) MUST raise `ValueError` naming the
  `budget_id`, unless `reset_caps=True`, which replaces the stored caps and keeps spend and open reservations. [T]
- **FR-050**: A `SharedCapBudget` passed to `Kernel(env, budget=...)` MUST behave as a `Budget` everywhere a
  `CapBudget` does today (LLM layer, `BudgetedProvider`, `budget.admit`/`budget.refuse` trace events with its
  snapshot figures, `kernel.usage.budget()`). `CapBudget` stays in-memory and per-process, and its docstring MUST
  say so. [T]

### I. Usage store under many writers

- **FR-051**: With N processes writing one usage store, no usage row may be lost: a write that fails on a locked
  database MUST be retried on the next write and, at `Kernel.close()` (or store close), retried until it lands or
  5 seconds pass. After every Kernel closed normally, `loss()["write_dropped"] == 0` and
  `loss()["pending_writes"] == 0`. [T][M]
- **FR-052**: With a shared data dir, `kernel.usage.total()` MUST report every call in that store (all Kernels and
  processes sharing it), and `kernel.usage.total(consumer=c)` exactly the calls attributed to consumer `c`. The sum
  of `requests` over all consumers MUST equal the ungrouped total. [T]

### J. Many Kernels and agent cores

- **FR-053**: `Environment.for_host` MUST accept `data_dir: Path | str | None = None`; when given,
  `env.paths.data_dir == Path(data_dir).resolve()` and the usage store is `<data_dir>/llm_usage.sqlite3`; when
  omitted, today's derivation (`<state_dir>/data`) holds. Under `strict=True`, a `data_dir` equal to or inside
  `work_dir` MUST raise `PathsOverlapError`. [T]
- **FR-054**: Two writer Kernels in one process on different state dirs MUST work concurrently: sessions, memory and
  agent state stay under their own state dir; closing one leaves the other fully usable (including its usage
  store when the data dir is shared). [T]
- **FR-055**: Two agents in one Kernel with distinct effective memory keys MUST use
  `<state_dir>/agents/<key>/memory` and default session `agent:<key>` each, with no file shared. Building a second
  live agent with the same effective memory key MUST emit a `kernel.memory_key_shared` trace event with fields
  `memory_key`, `agents` (both names); behaviour otherwise stays as today (a warning, not a refusal). [T]

### K. Containers

- **FR-056**: The repo MUST contain a root `Dockerfile` whose image runs as UID 1000 and GID 1000 by default, sets
  `MOEKA_WORKSPACE=/data/ws`, declares `/data` as a volume, contains no secret, and whose default command runs the
  gateway on `/data/ws/config.json` through an entrypoint script `scripts/container-entrypoint.sh`. [S][T]
- **FR-057**: The entrypoint MUST: exit 2 before starting anything when `MOEKA_TOKEN_ISSUE_SECRET` is unset or
  empty; exit non-zero with a message naming `/data` and UID 1000 when `/data` is not writable; on first start
  (no `/data/ws/config.json`) seed the config from `templates/workspace/config.json` with
  `agents.defaults.workspace = "/data/ws"`, `gateway.host = "0.0.0.0"`, `gateway.port = 18790`,
  `channels.websocket = {host: "0.0.0.0", port: 8765, tokenIssueSecret: "${MOEKA_TOKEN_ISSUE_SECRET}"}` and
  `providers.ollama.apiBase = "${MOEKA_OLLAMA_API_BASE}"`; default `MOEKA_OLLAMA_API_BASE` to
  `http://host.docker.internal:11434/v1`; never overwrite an existing config. Sessions then live in
  `/data/ws-sessions/`. [T]
- **FR-058**: The repo MUST contain a root `compose.yaml` with one service that runs as `1000:1000`, mounts a named
  volume at `/data`, publishes the gateway and WebSocket ports on `127.0.0.1` only with host ports from
  `MOEKA_GATEWAY_PORT` (default 18790) and `MOEKA_WS_PORT` (default 8765), requires `MOEKA_TOKEN_ISSUE_SECRET`
  (`${MOEKA_TOKEN_ISSUE_SECRET:?...}`), passes `MOEKA_OLLAMA_API_BASE`, maps `host.docker.internal` to
  `host-gateway`, drops all capabilities, sets `no-new-privileges`, and has a healthcheck on the gateway `/health`.
  Two projects (`docker compose -p a`, `-p b`) with different port variables MUST run at once with separate
  volumes. [S][T]
- **FR-059**: `docs/deployment.md` MUST describe only files that exist: no mention of `docker-compose.yml`,
  `docker-compose.bwrap.yml`, `/home/nanobot/.nanobot`, services `nanobot-gateway`/`nanobot-cli`, or the
  `NANOBOT_CHANNELS`/`NANOBOT_EXTRAS` build args unless the new files implement them; it MUST document the image,
  compose usage for several instances, the `0.0.0.0` plus `tokenIssueSecret` rule, the Ollama override, and that
  bwrap inside a container needs `CAP_SYS_ADMIN` and user namespaces. [D]

### L. Isolation floor

- **FR-060**: The file-tool floor (`ProtectedFloor`, `check_protected`) MUST deny read and write of any path whose
  last component is `keys.env`, and of `.env` directly inside any discovered instance root (own or other). [T]
- **FR-061**: The floor MUST deny read and write of every other instance's root and its sessions sibling.
  "Other" means a root that neither equals nor contains the agent's own work dir. Kernel-native hosts pass the
  roots: `ProtectedFloor.from_paths(paths, other_instance_roots=[...])` (and `ProtectedFloor(...,
  other_instance_roots=...)`); the kernel never discovers them. Legacy hosts (`check_protected(...,
  data_dir=None)`, the gateway's legacy environment) include the discovered instances computed from `$HOME` and the
  registry. [T]
- **FR-062**: The floor's docs and `docs/multiple-instances.md` MUST state that the floor covers the file tools
  only, that `exec` can still reach these paths, and that real separation between instances needs separate UIDs,
  the bwrap sandbox, or containers. [D]

### Key Entities

- **Instance**: root path, name, config path, unit name or none, ports (gateway, api, websocket-or-socket), PID file,
  gateway lock, keys file.
- **Instance lock**: `.instance.lock` plus holder record `.instance.json` (`pid`, `hostname`, `started_at`,
  `argv0`, `mode`).
- **Channel token lock**: lock and sidecar in the run dir, keyed by channel name and a 16-hex token digest.
- **Shared budget**: caps per `budget_id`; reservations (`call_id`, USD, tokens, tag slots, holder, created,
  lease expiry, state open/released/expired, unpriced-usage mark); spend totals per scope.

## Success Criteria *(mandatory)*

- **SC-001**: Two named host instances and two containers run at the same time for at least 60 seconds with zero
  port-bind errors, four distinct gateway health endpoints answering 200, and four distinct `sessions.db` files.
- **SC-002**: In 10 of 10 repetitions with three running instances, stopping one leaves the other two with the same
  PIDs and answering health checks, and the stopped one exits within `MOEKA_STOP_TIMEOUT_S + 5` seconds.
- **SC-003**: In 20 of 20 attempts (10 in-process, 10 cross-process) a second writer on a locked state dir is refused
  within 2 seconds and writes zero bytes under the state dir.
- **SC-004**: In 10 randomized runs of 4 processes x 50 concurrent calls against one USD cap, the maximum observed
  `spent_usd + reserved_usd` never exceeds the cap by more than 1e-9 USD, and final `spent_usd <= cap`.
- **SC-005**: 4 processes x 50 fake calls on one data dir give exactly 200 usage requests, per-consumer sums equal
  to the total, and `write_dropped == 0`.
- **SC-006**: After SIGKILL of a holder, a new writer acquires the instance lock within 2 seconds, and an expired
  budget reservation is reconciled within `lease_s + 1` seconds.
- **SC-007**: Five consecutive `moeka.sh new` runs produce five configs with pairwise-disjoint port sets that are
  also disjoint from the default instance, each in under 5 seconds.
- **SC-008**: 100% of a fixed matrix of at least 20 protected paths are denied for read and write, and 100% of at
  least 10 control paths in the own work dir are allowed.
- **SC-009**: The full existing suite (`scripts/test-docker.sh`) stays green, and throughout the work the live
  checkout's `git status` output and the bytes of `~/.config/systemd/user/moeka.service` are unchanged.

## Owner Decisions

- **Q1** [NEEDS CLARIFICATION: when a call times out or is cancelled, does the budget charge the full remaining
  reservation or only an input-token estimate?] **Default (tests assume this)**: the full remaining reservation is
  charged (today's `CapBudget` rule; never undercounts). An expired lease (FR-047) is charged the same way.
- **Q2** [NEEDS CLARIFICATION: is a one-shot `nanobot agent -m` or `nanobot serve` on a workspace whose gateway is
  running refused, or allowed to share the state under SQLite locking only?] **Default (tests assume this)**:
  refused with exit 3 (FR-033/FR-034); the interactive `nanobot agent`, which talks to the running gateway, is
  unaffected.
- **Q3** [NEEDS CLARIFICATION: does the default instance keep auto-loading the repo-level `.env`/`keys.env`?]
  **Default (tests assume this)**: the repo files load only for the default instance (root `$HOME/.nanobot`) or
  when `MOEKA_REPO_ENV=1`; named and registered instances never load them otherwise.

## Assumptions

- One host, one user UID for host instances; Linux with `flock(2)`; containers on the same host share the kernel
  clock. Multi-host coordination is out of scope.
- The data dir and state dirs are on a local filesystem (not NFS/SMB).
- The gateway stays on the legacy `AgentLoop` path (stage 2 of spec 003 is out of scope); the instance lock is taken
  in the gateway command itself.
- The live checkout (`~/projects/moeka`) and `moeka.service` are not touched by this feature; adoption is the M5
  cutover (owner gate).
- `CapBudget` stays as the in-memory reference; `SharedCapBudget` is additive.
- Upstream nanobot's own multi-instance docs are replaced, not kept side by side.

## Test Harness Contract

Hidden tests MUST use only these surfaces. Everything here is either existing public behaviour or required by an FR
above. Tests MUST run offline (no network, no real model, no real bot token) and MUST NOT touch the real `$HOME`,
the live checkout, or real systemd units.

### Environment variables (tests may set)

| Variable | Meaning |
|----------|---------|
| `HOME` | Always a temporary directory in tests; all instance roots, the registry and unit files derive from it. |
| `MOEKA_WORKSPACE` | Instance root (FR-001). |
| `MOEKA_CONFIG` | Config path override for `moeka.sh`. |
| `MOEKA_NANOBOT_BIN` | Executable `moeka.sh` runs instead of `.venv/bin/nanobot`; when set, no venv is created. |
| `MOEKA_SYSTEMCTL` | Command `moeka.sh` and `install-service.sh` call instead of `systemctl` (a fake that logs its argv and answers `is-active` with exit 3 unless told otherwise). |
| `MOEKA_LOGINCTL` | Command called instead of `loginctl`. |
| `MOEKA_STOP_TIMEOUT_S` | Grace period before SIGKILL in `stop` (default 10). |
| `MOEKA_RUN_DIR` | Run dir for channel and `new` locks. |
| `MOEKA_REPO_ENV` | `1` loads repo-level env files for any instance (Q3). |
| `MOEKA_TOKEN_ISSUE_SECRET`, `MOEKA_OLLAMA_API_BASE`, `MOEKA_GATEWAY_PORT`, `MOEKA_WS_PORT` | Container inputs (FR-057, FR-058). |

`python3` and `bash` are on `PATH`; `procps` may be absent.

### CLI surfaces and exit codes

- `bin/moeka.sh [--workspace P] [--config F] <cmd>`: `new NAME [--workspace P] [--port-base N] [--ws-tcp]`,
  `start`, `run`, `stop`, `restart`, `status [--json]`, `list [--json]`, `logs`, `doctor`, `enable`, `disable`,
  `telegram-pair`. Exit codes: 0 ok (also "already running", "nothing to stop", duplicate `run`), 1 runtime
  failure (target not empty, port taken, missing config), 2 usage error (bad name, bad port base, unexpanded
  workspace, socket path too long), 3 not running (`status`) or locked/refused.
- The fake gateway started via `MOEKA_NANOBOT_BIN` receives argv `gateway --config <abs config> [--workspace
  <abs root>] ...`; `stop` matches it by the config path in `/proc/<pid>/cmdline`.
- `scripts/install-service.sh [<name>] [--no-enable] [--dry-run]`: exit 0 ok, 1 missing instance, 2 bad name.
- `nanobot gateway|serve|agent -m ... --config F [--workspace P]`: exit 2 unexpanded workspace, 3 instance locked.
- Container: `docker build -t <tag> .` at the repo root; `docker run --rm --entrypoint id <tag> -u`;
  `docker compose -p <proj> up -d` / `down -v` with the variables above. Container tests are marked and skipped
  when `docker` is unavailable.

### Python APIs

- `from moeka import Environment, Kernel, ModelSpec, ProviderSpec`;
  `Environment.for_host(*, state_dir, work_dir, credentials, providers, models, default_model, data_dir=None,
  trace=None, strict=True, offline=False, ...)`;
  `Kernel(env, *, budget=None, consumer=None, attach="write", ...)` with `close()`, `aclose()`, context managers.
- `from moeka.testing import FakeProvider, reply, error`; `kernel.llm.register_provider(alias, fake, spec)`;
  `kernel.llm.complete(...)` as documented in `docs/python-sdk.md`.
- `from moeka.budget import CapBudget, SharedCapBudget`; `from moeka.errors import BudgetExceeded,
  InstanceLockedError, ReadOnlyKernelError` (`BudgetExceeded.reason_code`, `InstanceLockedError.state_dir`,
  `.holder`, `.reason`).
- `kernel.usage.total(**filters)` (fields `requests`, `calls`, `cost_usd`, ...), `kernel.usage.loss()`,
  `kernel.usage.budget()`; `kernel.sessions.get/list/open`; `kernel.memory(scope)`; `kernel.agent(AgentSpec(...))`
  with `from nanobot.kernel.agent import AgentSpec`.
- Trace events are observed through the `trace=` sink given to `Environment.for_host`; names used here:
  `budget.admit`, `budget.refuse`, `budget.expire`, `kernel.memory_key_shared`.
- `nanobot.config.schema.UnexpandedWorkspaceError`; `nanobot.config.loader.{load_config, set_config_path,
  pin_config_path, reset_config_path, ConfigPathConflictError}`; `nanobot.config.paths.{get_cli_history_path,
  get_bridge_install_dir, get_legacy_sessions_dir, get_state_home, get_data_dir}`.
- `nanobot.kernel.legacy.LegacyEnvironment.from_config(config)` -> `.paths`.
- `nanobot.gateway.runtime.GatewayInstance.resolve(config_path=..., workspace=None)`;
  `nanobot.gateway.service.GatewayServiceInstaller(platform_name="Darwin"|"Linux", home=..., subprocess_run=...)`
  with `.install(GatewayServiceOptions(start=GatewayStartOptions(...)), dry_run=True)` -> `.content`.
- `nanobot.channels.token_lock.{acquire_channel_token_lock, ChannelTokenInUseError}`;
  `nanobot.channels.manager.ChannelManager.get_status()`;
  `nanobot.channels.telegram.runtime.TelegramChannel(config, bus)` with `.handle_polling_error(exc)`,
  `.polling_state`, `.conflict_retry_s`; `telegram.error.Conflict` as the input.
- `nanobot.security.protected_paths.{ProtectedFloor, check_protected, PROTECTED_MARKER, ProtectedPathError}`;
  `ProtectedFloor.from_paths(paths, other_instance_roots=...)`; `Paths` from `moeka`.

### File locations (tests may read, never write except where noted)

- Instance: `<root>/config.json`, `<root>/keys.env`, `<root>/moeka.pid`, `<root>/gateway.lock`, `<root>/moeka.log`,
  `<root>/run/websocket.sock`, `<root>-sessions/<id>/sessions.db`.
- Locks: `<state_dir>/.instance.lock`, `<state_dir>/.instance.json`,
  `<run dir>/channel-locks/<channel>-<16hex>.lock` and `.json`.
- Registry: `$HOME/.config/moeka/instances`. Units: `$HOME/.config/systemd/user/moeka@.service`.
- Usage and budget: `<data_dir>/llm_usage.sqlite3` (tests check existence only; figures come from the APIs above).
- Repo files checked statically: `scripts/moeka@.service`, `scripts/install-service.sh`, `Dockerfile`,
  `compose.yaml`, `scripts/container-entrypoint.sh`, `docs/deployment.md`, `docs/multiple-instances.md`.
