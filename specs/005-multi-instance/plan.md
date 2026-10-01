# Implementation Plan: Multi-Instance moeka (no singleton)

**Branch**: `005-multi-instance` | **Date**: 2026-10-01 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/005-multi-instance/spec.md`; master plan section M3.

## Summary

Make every moeka resource instance-scoped. The shell control script acts on one instance by its PID file, lock
and unit; new instances get their own ports and a Unix WebSocket socket; a template unit runs named instances; an
unexpanded workspace fails fast; config-owned paths replace process globals; an OS lock per state dir admits one
writer (others refused or attached read-only); a lock per bot token and `Conflict` handling stop double polling;
`SharedCapBudget` keeps caps across processes in the shared SQLite usage store; many Kernels per process and per
data dir are specified and tested; a minimal image and compose file run instances in containers; and the file-tool
floor hides other instances and every `keys.env`.

## Technical Context

**Language/Version**: Python 3.11+ (tests run on 3.13 in `Dockerfile.test`); bash for `bin/moeka.sh` and scripts.

**Primary Dependencies**: `filelock` (already a dependency, used by `gateway/runtime.py` and `session/sqlite_store.py`),
stdlib `sqlite3`, `fcntl`; `python-telegram-bot` (`telegram.error.Conflict`); Docker + Compose v2 for containers.

**Storage**: SQLite WAL: `<data_dir>/llm_usage.sqlite3` (usage, plus new budget tables), `sessions.db` per instance;
lock files on the local filesystem.

**Testing**: pytest (`asyncio_mode=auto`) via `scripts/test-docker.sh`; bash scripts driven from pytest with a fake
`HOME`, fake `nanobot` and fake `systemctl`; container tests marked `docker` and skipped without Docker.

**Target Platform**: Linux user sessions with systemd user units; Docker on the same host.

**Project Type**: library (kernel) + CLI/gateway host + ops scripts.

**Performance Goals**: admission adds at most one short SQLite write transaction per logical call (target p95 under
20 ms uncontended); lock acquisition or refusal within 2 s.

**Constraints**: the live checkout `~/projects/moeka` and `~/.config/systemd/user/moeka.service` are never touched;
work happens in worktree `~/projects/moeka-spec-005` (spec) and the implementer's own worktree; kernel code keeps
zero ambient reads (constitution I).

**Scale/Scope**: tens of instances per host; up to tens of processes per data dir.

## Facts about current code (as of 2026-10-01, `consolidate/new-main` working tree with the M1 merge staged)

Verified by reading `/home/muk/projects/moeka-newmain`:

- `bin/moeka.sh`:
  - `cmd_stop` ends with `pkill -f "nanobot gateway"` (line 259), which kills every instance's gateway.
  - `cmd_doctor` falls back to `pgrep -f 'nanobot gateway' | head -1` (line 497).
  - `cmd_run` (lines 162-182) holds `flock` on `<ws>/gateway.lock` but passes only `--config`.
  - The unexpanded-`${` guard (lines 88-96) silently falls back to `~/.nanobot`.
  - Repo `.env`/`keys.env` load unconditionally (lines 98-99).
  - `cmd_telegram_pair` writes to `${SCRIPT_DIR}/keys.env` (line 700).
  - `cmd_new` copies `templates/workspace` unchanged, so every new instance gets gateway 17380, API 8900 and, via the
    always-on WebSocket channel, TCP 8765.
  - systemd checks hard-code units `moeka` and `nanobot`.
- `scripts/moeka.service` hard-codes `WorkingDirectory=%h/projects/moeka` and repo `EnvironmentFile`s.
  `scripts/install-service.sh` installs only that unit and removes `nanobot.service`.
- `templates/workspace/config.json` has `"workspace": "${MOEKA_WORKSPACE}"`, `gateway.port` 17380, `api.port` 8900,
  and no `channels.websocket` section. `WebSocketConfig` (`channels/websocket/runtime.py:174`) already supports
  `unix_socket_path` (absolute, validated) and requires a token or `token_issue_secret` on `0.0.0.0`. The websocket
  plugin is `default_enabled=True` with `always_enabled`.
- `nanobot/config/schema.py:624-636` `Config.workspace_path` returns `get_state_home()` when the value contains `${`.
  `loader.py:27` keeps the process global `_current_config_path`; `set_config_path` (line 36) rebinds it freely.
  `paths.py:118-130`: `get_cli_history_path`, `get_bridge_install_dir` and `get_legacy_sessions_dir` derive from
  `get_state_home()` (env chain), not from the config directory.
- `nanobot/kernel/legacy.py:320-341` `_legacy_paths` uses `_legacy_data_dir` = `loader.get_config_path().parent`
  (process global) for data/media/logs when the config came from a file.
- `nanobot/gateway/runtime.py:69-70` `_default_config_path()` is the literal `~/.nanobot/config.json`.
  `gateway/service.py:139-140` launchd logs are under the literal `~/.nanobot/logs`.
- `nanobot/kernel/kernel.py`:
  - `Kernel.__init__` acquires the usage store per `env.core.paths.data_dir` (lines 144-149).
  - It takes no state-dir lock.
  - `Kernel.agent` only logs a warning on a shared memory key (lines 340-351).
- `nanobot/kernel/hostenv.py:229` `Environment.for_host` has no `data_dir` parameter. `Paths` (`kernel/env.py`)
  already supports `data_dir_override`.
- `nanobot/kernel/budget.py`:
  - `CapBudget` (lines 259-465) is a thread-safe, in-memory object.
  - `settle` already treats `timeout`/`cancelled` outcomes and unpriced tokens as "charge the remainder at release".
- `nanobot/llm_usage/store.py`: one WAL database per data dir, `busy_timeout=250`. Failed writes are deferred in a
  per-process deque (`MAX_PENDING=1000`) and retried only on the next write; `close()` does not flush them.
- `nanobot/session/sqlite_store.py:115` `default_sessions_root` = `<ws>-sessions`. Only a migration `FileLock`
  exists; no writer exclusivity.
- `nanobot/channels/telegram/runtime.py`:
  - `_on_polling_error` (line 2139) logs every non-network error, `Conflict` included, at ERROR with no state change.
  - `start_polling` passes it as `error_callback` (line 769).
  - No token lock exists.
- `nanobot/security/protected_paths.py`:
  - The floor covers `auth/`, `plugin-data/`, `sessions/`, the usage DB, sessions roots and kernel state files.
  - It does not cover `keys.env`, other instances, or lock and PID files.
- `docs/deployment.md` Docker sections (lines 59-227) describe `Dockerfile`, `docker-compose.yml`,
  `docker-compose.bwrap.yml`, `/home/nanobot/.nanobot` and services `nanobot-gateway`/`nanobot-cli`. None of these
  exist in the repo (only `Dockerfile.test`).
- `docs/multiple-instances.md` is upstream text (`--config` per instance, "each instance must use a different
  port") with no lock, port or budget rules.

## Constitution Check

*GATE: checked before design and after design.*

| Principle | Status | How |
|-----------|--------|-----|
| I Zero ambient reads | Pass | The instance lock, read-only attach and `SharedCapBudget` take every path from `env.paths` or arguments. Instance discovery, run-dir resolution (`MOEKA_RUN_DIR`/`XDG_RUNTIME_DIR`) and `$HOME` scanning live only in host modules (`nanobot/config/instances.py`, channel manager, legacy adapter, CLI), which are added to the reviewed `HOST_AMBIENT_ALLOWLIST`. The kernel floor takes `other_instance_roots` as an argument. |
| II Physical separation | Pass | `data_dir` inside `work_dir` is refused under strict. FR-062 states that the floor covers file tools only and that containers or bwrap are the real boundary. |
| III Unearned knowledge | N/A | No artifact commits. |
| IV Attenuation | Pass | The floor only grows (new denied roots). Floors cannot be removed by config. |
| V Hard limits | Pass | Budget refusal stays typed (`BudgetExceeded.reason_code`). A declared lock that cannot be taken refuses (`lock_unsupported`) instead of running unlocked. |
| VI Cost ceiling | Pass | No model calls added. Admission cost is measured (performance goal). |
| VII Everything is data | Pass | Observation stays fail-open (usage writes retried, never blocking a call beyond the close-time flush). The budget is enforcement, not observation, so it fails closed (`budget_unavailable`). New `budget.expire` and `kernel.memory_key_shared` events. Zero dropped rows is an FR. |
| VIII Verification independence | Pass | Held-out tests from the spec only. |
| IX Consumers derive usage from moeka | Pass | `SharedCapBudget` and `kernel.usage` are public API. Consumers and tests never read SQLite tables. |
| X Cite verified sources | N/A | No citations. |
| Requirements vs facts | Pass | Code facts are only in this plan and tasks, dated. |
| Live service protection | Pass | All work is in worktrees. SC-009 checks the live checkout and unit bytes. |

## Design

### Shell layer (`bin/moeka.sh`, scripts)

- Instance resolution:
  - Resolve the instance first. Detect it by root: the default instance is `$HOME/.nanobot`, a named instance
    matches `^$HOME/.moeka-([a-z0-9][a-z0-9_-]{0,31})$`, and anything else is registered.
  - Map each kind to a unit name.
  - Every systemctl call goes through `${MOEKA_SYSTEMCTL:-systemctl}`.
- Liveness check:
  - Read the PID file, then `/proc/<pid>/cmdline` (NUL-split).
  - Test for the config path, then test the lock with `flock -n` on a dup'd fd.
  - No `ps`/`pgrep`. Uptime comes from `/proc/<pid>/stat` when available.
- Environment and run:
  - Env loading follows FR-003 and Q3.
  - `run` does `echo $$ > moeka.pid` before `exec`, so the PID survives exec.
- Discovery and `list`:
  - Implemented once in `nanobot/config/instances.py`, a stdlib-only module with no `nanobot` imports.
  - Host Python code imports it. `moeka.sh` runs it by file path (`python3 "$SCRIPT_DIR/nanobot/config/instances.py"
    list --json`), so it works without the venv, which matters for fake-bin tests.
  - The shell, the gateway and the legacy floor therefore share one set of rules.
- Port selection:
  - Lives in that same helper, under `flock` on `<run dir>/new.lock`.
  - Bind tests use `socket.bind(("127.0.0.1", p))` then close.
- Template unit and installer:
  - `scripts/moeka@.service` carries `@MOEKA_REPO@`. `install-service.sh <name>` renders it with `sed`.
  - Without a name, the current flow is untouched.

### Fail fast and config-owned paths

- `UnexpandedWorkspaceError(ValueError)` is raised in `Config.workspace_path`.
- CLI entry points map it to exit 2 before any `ensure_dir`.
- Audit call sites that create the workspace eagerly (`get_workspace_path`, `SqliteSessionStore`) so none run
  before the check.
- `_legacy_paths`: replace `_legacy_data_dir` with `lambda: Path(config.runtime_data_dir)` when
  `runtime_data_dir` is set.
- `_default_config_path()` becomes `(get_state_home() / "config.json").resolve(strict=False)`.
- launchd log dir:
  - Add a helper `_instance_data_dir(options.start)` and use it for the log dir.
  - Keep `_working_directory` as is, apart from using the same helper for the fallback.
- `paths.py` helpers gain `data_dir: Path | None = None`. Callers in `cli/` that hold a config pass
  `config.runtime_data_dir`.
- `pin_config_path`/`reset_config_path`/`ConfigPathConflictError` in `loader.py`. The gateway command calls `pin`.

### Instance lock (`nanobot/kernel/instance_lock.py`)

- `acquire_instance_lock(state_dir: Path, *, mode="write") -> InstanceLock`:
  - Opens `<state_dir>/.instance.lock` with `O_CREAT`, then `fcntl.flock(LOCK_EX | LOCK_NB)`.
  - `EWOULDBLOCK` maps to `reason="held"`. `ENOLCK`/`EOPNOTSUPP`/`EINVAL` map to `reason="lock_unsupported"`.
- In-process second writer:
  - Keep a module registry `{resolved_state_dir: InstanceLock}` guarded by a `threading.Lock`.
  - A second writer is refused before touching the fd. `flock` locks on separate fds in one process would also
    conflict, but the registry gives a deterministic message.
- Holder record: `.instance.json` is written atomically (tmp + rename) after the lock is taken, and read
  best-effort for messages.
- `InstanceLockedError(RuntimeError)` carries `state_dir`, `holder` and `reason`. `ReadOnlyKernelError(RuntimeError)`
  is new. Both are re-exported in `moeka.errors`.
- Kernel wiring:
  - `Kernel.__init__` takes the lock first, before `acquire_llm_usage_store` and before anything that creates
    directories.
  - `close()` releases it last.
- `attach="read_only"`:
  - Sets `self._read_only`.
  - Guards the write entry points of `kernel.sessions`, `Session`, `DocStore` and `kernel.agent`.
  - Opens SQLite stores with `file:...?mode=ro` URIs.
  - Must not call `ensure_dir`. The sessions store needs a read-only constructor path that skips the workspace-id
    claim and legacy import.
- Gateway, `serve` and one-shot agent: take the lock on `config.workspace_path` right after config load (the flat
  layout's state dir), and exit 3 on refusal.

### Channel token lock and Telegram `Conflict`

- `nanobot/channels/token_lock.py`:
  - Run-dir resolution, `<channel>-<sha256[:16]>.lock`, `flock`, a JSON sidecar, and an in-process registry for
    determinism.
  - `ChannelManager` acquires it in its per-channel start path (before `channel.start()`). On refusal it records
    `state="locked"` in a new `_channel_locked` map that `get_status` consults first.
- `TelegramChannel`:
  - Rename `_on_polling_error` to the public `handle_polling_error` (keep the alias).
  - On `Conflict`: set `polling_state="conflict"`, log once per episode, and schedule `updater.stop()` plus a
    restart after `conflict_retry_s` via the existing `_idle`/restart loop.
  - `_note_poll_ok` ends the episode.
  - Add `conflict_retry_s` (alias `conflictRetryS`, `Field(default=60, ge=5, le=3600)`) to the Telegram config.
  - The manager maps `polling_state == "conflict"` to `state="conflict"`.

### `SharedCapBudget` (`nanobot/kernel/budget_shared.py`, exported from `moeka.budget`)

- Tables, created in `<data_dir>/llm_usage.sqlite3` through the usage store's migration path (additive only):
  - `budget_caps(budget_id TEXT PRIMARY KEY, limit_usd REAL, limit_tokens INTEGER, per_tag TEXT,
    allow_unpriced INTEGER, updated_ms INTEGER)`.
  - `budget_reservations(id INTEGER PRIMARY KEY, budget_id TEXT, call_id TEXT, usd REAL, tokens INTEGER,
    slots TEXT, holder TEXT, created_ms INTEGER, lease_expires_ms INTEGER, state TEXT, unpriced INTEGER,
    expired_charge_usd REAL, expired_charge_tokens INTEGER)`.
  - `budget_spend(budget_id TEXT, scope_key TEXT, scope_value TEXT, spent_usd REAL, spent_tokens INTEGER,
    PRIMARY KEY(budget_id, scope_key, scope_value))`. The kernel scope uses `scope_key=''`.
- Connection: own connection per object per pid, `isolation_level=None`, `BEGIN IMMEDIATE`, and
  `busy_timeout = lock_timeout_s*1000`. Admission maps a `database is locked` after the timeout to
  `budget_unavailable`.
- Admission algorithm (one transaction):
  1. `UPDATE ... SET state='expired'` for open rows past their lease. Add their remaining usd and tokens to
     `budget_spend` (kernel and tag scopes) and record `expired_charge_*`.
  2. Read spend and the sums of open reservations per scope.
  3. Apply `CapBudget.admit`'s checks verbatim. Factor them into a pure function shared by both classes so the
     arithmetic cannot drift.
  4. Insert the reservation.
- Settle and release use the same pure functions on the row inside a transaction.
- Expired rows (true-up):
  - Settle takes `min(cost, expired_charge_remaining)` from the charge. Only the excess adds to spend.
  - Release without unpriced usage refunds the remaining charge. Release with unpriced usage keeps it.
- Failed writes:
  - A settle or release that fails to commit is queued on the object and replayed in order at the start of the
    next operation.
  - `Kernel.close()` calls `budget.flush()` when the budget has one.
- `holder` is `hostname:pid:start_ms`, used only for diagnostics.

### Usage store flush

- Make `LLMUsageStore.close()` flush `_pending` with a deadline of 5 s, retrying `BEGIN IMMEDIATE` with backoff.
- Make the busy timeout used for retries at close longer than the hot-path 250 ms.
- M4 (moving writes off the event loop) is out of scope. Do not regress hot-path latency.

### Many Kernels

- `Environment.for_host(data_dir=...)` sets `Paths(data_dir_override=...)`, plus the strict overlap check against
  `work_dir`.
- `kernel.memory_key_shared`: emit through `safe_emit(self._tracer, ...)` next to the existing warning.

### Containers

- `Dockerfile` (root):
  - Base `python:3.13-slim`, plus `uv`. Install the project non-editable into `/opt/venv` with `uv sync --no-dev`
    (no `vec` extra by default, `ARG EXTRAS`).
  - `groupadd -g 1000 moeka && useradd -u 1000 -g 1000 -m moeka`, then `USER 1000:1000`.
  - `ENV MOEKA_WORKSPACE=/data/ws`, `VOLUME /data`, `EXPOSE 18790 8765`.
  - `ENTRYPOINT ["/usr/local/bin/moeka-entrypoint"]`, `CMD ["gateway", "--config", "/data/ws/config.json",
    "--workspace", "/data/ws"]`.
  - `HEALTHCHECK` with python urllib against `127.0.0.1:18790/health`.
- `scripts/container-entrypoint.sh`:
  - Checks in FR-057 order, then seeds the config with a python snippet, then `exec nanobot "$@"`.
- `compose.yaml`:
  - Service `moeka`, `user: "1000:1000"`, `volumes: [moeka-data:/data]`.
  - Ports `127.0.0.1:${MOEKA_GATEWAY_PORT:-18790}:18790` and `127.0.0.1:${MOEKA_WS_PORT:-8765}:8765`.
  - `environment` with `MOEKA_TOKEN_ISSUE_SECRET: ${MOEKA_TOKEN_ISSUE_SECRET:?set MOEKA_TOKEN_ISSUE_SECRET}` and
    `MOEKA_OLLAMA_API_BASE`.
  - Optional `env_file` with `required: false` for keys.
  - `extra_hosts: ["host.docker.internal:host-gateway"]`, `cap_drop: [ALL]`,
    `security_opt: [no-new-privileges:true]`, a `healthcheck`, and `volumes: {moeka-data: {}}`.
- `docs/deployment.md`: rewrite the Docker sections. Keep the Cloudflare section, since it is not Docker-specific.

### Isolation floor

- Matching:
  - `_match` gains a basename rule for `keys.env`.
  - The constructor gains `other_instance_roots`. Each root R adds R and `R-sessions` to the denied roots, plus
    `R/.env`.
- Own roots (work dir and data dir): add `<root>/.env`, `<root>/moeka.pid`, `<root>/gateway.lock`, `<root>/run`
  and the state dir's `.instance.lock`/`.instance.json` as write-denied.
- Legacy path: `legacy_floor_extras()` adds the discovered roots minus the own one (it is host-side, so ambient is
  allowed).

## Project Structure

### Documentation (this feature)

```text
specs/005-multi-instance/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
bin/moeka.sh                                  # per-instance control, new/list/status --json
scripts/moeka@.service                        # new template unit
scripts/install-service.sh                    # <name> mode
scripts/container-entrypoint.sh               # new
Dockerfile                                    # new
compose.yaml                                  # new
templates/workspace/config.json               # unchanged placeholder; `new` rewrites the copy
nanobot/config/schema.py                      # UnexpandedWorkspaceError
nanobot/config/loader.py                      # pin/reset config path
nanobot/config/paths.py                       # data_dir-aware helpers
nanobot/config/instances.py                   # stdlib-only discovery + port selection (new)
nanobot/kernel/instance_lock.py               # new
nanobot/kernel/kernel.py                      # lock, attach, memory_key_shared event, budget flush
nanobot/kernel/hostenv.py                     # for_host(data_dir=)
nanobot/kernel/legacy.py                      # _legacy_paths from config.runtime_data_dir
nanobot/kernel/sessions.py, memory.py         # read-only guards
nanobot/session/sqlite_store.py               # read-only open path
nanobot/kernel/budget.py                      # shared pure admit/settle arithmetic; CapBudget docstring
nanobot/kernel/budget_shared.py               # SharedCapBudget (new)
nanobot/llm_usage/store.py                    # budget tables migration, close-time flush
nanobot/gateway/runtime.py, service.py        # default identity, launchd logs
nanobot/channels/token_lock.py                # new
nanobot/channels/manager.py                   # token lock + locked/conflict states
nanobot/channels/telegram/runtime.py          # handle_polling_error, conflict state, conflictRetryS
nanobot/security/protected_paths.py           # keys.env, other instances, lock files
nanobot/cli/commands.py, gateway.py           # exit codes 2/3, lock, pin
moeka/budget.py, moeka/errors.py              # exports
docs/multiple-instances.md, docs/deployment.md
tests/...                                     # implementer's own tests (held-out tests live outside the tree)
```

**Structure Decision**: single repository; changes are spread by layer as above, and every new module sits next to
its closest existing peer.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Budget fails closed while observation is fail-open (VII) | A cap that admits when it cannot read state is not a cap; the owner rules depend on caps holding | Fail-open admission lets N processes overshoot exactly when the DB is contended |
| Default instance keeps repo env files (Q3 default) | The cutover (M5) must not lose the live bot's keys | Opt-in everywhere breaks the default instance on first restart after cutover |

## Risks

- Existing tests that open two Kernels on one state dir, or that assert the `${` fallback, will fail and must be
  updated (they encode the singleton behaviour this spec removes); list them in the implementer's report.
- The `Paths` callable override in `_legacy_paths` is evaluated lazily; `runtime_data_dir` must be captured at
  build time, not re-read from the process global.
- SQLite `BEGIN IMMEDIATE` across containers requires the same host and a local bind mount or named volume; document
  it.
- M1 (review/usage merge) touches `budget.py`, `kernel.py`, `env.py` and `CLARIFY-LOG.md`; start implementation
  from the post-M1 `consolidate/new-main` and fold Q1-Q3 into `specs/CLARIFY-LOG.md` (as `005-Q1..Q3`) after M1
  lands, so the log numbering does not conflict.
