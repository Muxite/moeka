#!/usr/bin/env bash
# Moeka — nanobot for server management. Every command acts on ONE instance.
#
# Usage:
#   ./bin/moeka.sh start            # start this instance's gateway in the background
#   ./bin/moeka.sh run              # run the gateway in the foreground (systemd ExecStart)
#   ./bin/moeka.sh stop             # stop this instance only (its unit and/or its PID file)
#   ./bin/moeka.sh restart          # stop + start
#   ./bin/moeka.sh status [--json]  # this instance's state (exit 0 running, 3 not running)
#   ./bin/moeka.sh list [--json]    # every discovered instance on this machine
#   ./bin/moeka.sh logs [-f] [-n N] # this instance's journal or <root>/moeka.log
#   ./bin/moeka.sh shell            # drop into the moeka venv
#   ./bin/moeka.sh exec -- CMD ...  # run a command inside the venv
#   ./bin/moeka.sh install          # install Python deps into .venv, show version
#   ./bin/moeka.sh version          # print installed moeka version
#   ./bin/moeka.sh doctor           # sanity check: runtime, config, api keys, service state
#   ./bin/moeka.sh enable           # install + enable this instance's systemd user unit
#   ./bin/moeka.sh disable          # stop + disable this instance's systemd user unit
#   ./bin/moeka.sh export [--out F] # bundle workspace into a portable archive
#   ./bin/moeka.sh import FILE      # extract a workspace archive into the instance root
#   ./bin/moeka.sh new NAME [--workspace P] [--port-base N] [--ws-tcp]
#                                   # scaffold an instance (own ports + Unix WebSocket socket)
#   ./bin/moeka.sh telegram-pair    # pair a Telegram bot token into <root>/keys.env
#
# Flags (anywhere on the command line):
#   --config PATH       override the config file path (default <root>/config.json)
#   --workspace PATH    the instance root (else MOEKA_WORKSPACE, else ~/.nanobot)
#
# Exit codes: 0 ok (also "already running", "nothing to stop"), 1 runtime failure,
# 2 usage error (bad name/port base, unexpanded workspace, socket path too long),
# 3 not running (status) or locked/refused.
#
# Environment: MOEKA_NANOBOT_BIN (gateway binary, no venv is created when set),
# MOEKA_SYSTEMCTL / MOEKA_LOGINCTL (commands used instead of systemctl/loginctl),
# MOEKA_STOP_TIMEOUT_S (grace period before SIGKILL, default 10), MOEKA_RUN_DIR (lock
# dir for `new`), MOEKA_REPO_ENV=1 (load the repo .env/keys.env for any instance).

set -euo pipefail

# ---------- paths & constants -----------------------------------------------
ORIG_PWD="$PWD"
# This script lives in bin/; SCRIPT_DIR is the *repo root* (its parent).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/.venv"
INSTANCES_PY="${SCRIPT_DIR}/nanobot/config/instances.py"

# ---------- tiny logging helpers (all on stderr: stdout is for data) ---------
if [ -t 2 ]; then
    _C_BLUE=$'\033[34m'; _C_GREEN=$'\033[32m'; _C_YELLOW=$'\033[33m'
    _C_RED=$'\033[31m'; _C_DIM=$'\033[2m'; _C_RESET=$'\033[0m'
else
    _C_BLUE=""; _C_GREEN=""; _C_YELLOW=""; _C_RED=""; _C_DIM=""; _C_RESET=""
fi
info() { printf '%s[moeka]%s %s\n' "$_C_BLUE" "$_C_RESET" "$*" >&2; }
ok()   { printf '%s[moeka]%s %s\n' "$_C_GREEN" "$_C_RESET" "$*" >&2; }
warn() { printf '%s[moeka]%s %s\n' "$_C_YELLOW" "$_C_RESET" "$*" >&2; }
err()  { printf '%s[moeka]%s %s\n' "$_C_RED" "$_C_RESET" "$*" >&2; }

# ---------- argv parsing ----------------------------------------------------
CONFIG_OVERRIDE=""
WORKSPACE_OVERRIDE=""
WORKSPACE_GIVEN=0
POSITIONAL=()

while (( $# > 0 )); do
    case "$1" in
        --config)
            [[ $# -lt 2 ]] && { err "--config requires a path"; exit 2; }
            CONFIG_OVERRIDE="$2"; shift 2 ;;
        --config=*) CONFIG_OVERRIDE="${1#--config=}"; shift ;;
        --workspace)
            [[ $# -lt 2 ]] && { err "--workspace requires a path"; exit 2; }
            WORKSPACE_OVERRIDE="$2"; WORKSPACE_GIVEN=1; shift 2 ;;
        --workspace=*) WORKSPACE_OVERRIDE="${1#--workspace=}"; WORKSPACE_GIVEN=1; shift ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"
CMD="${1:-help}"
[[ $# -gt 0 ]] && shift || true

# ---------- instance resolution (FR-001, FR-002) -----------------------------
# Decided BEFORE any env file is loaded: --workspace, else MOEKA_WORKSPACE, else
# ~/.nanobot. An env file cannot move the instance afterwards.
_expand_tilde() {
    local v="$1"
    case "$v" in
        "~") v="$HOME" ;;
        "~/"*) v="$HOME/${v:2}" ;;
    esac
    printf '%s' "$v"
}

_normalize_path() {
    # Absolute (relative to the caller's cwd), without resolving symlinks.
    local v="$1"
    case "$v" in
        /*) ;;
        *) v="$ORIG_PWD/$v" ;;
    esac
    if command -v realpath >/dev/null 2>&1; then
        realpath -ms -- "$v" 2>/dev/null || printf '%s' "$v"
    else
        printf '%s' "$v"
    fi
}

if (( WORKSPACE_GIVEN )); then
    _ROOT_RAW="$WORKSPACE_OVERRIDE"
elif [[ -n "${MOEKA_WORKSPACE:-}" ]]; then
    _ROOT_RAW="$MOEKA_WORKSPACE"
else
    _ROOT_RAW="$HOME/.nanobot"
fi
ROOT="$(_expand_tilde "$_ROOT_RAW")"
if [[ "$ROOT" == *'${'* ]]; then
    err "MOEKA_WORKSPACE (instance root) has an unexpanded variable: '$ROOT'"
    err "set MOEKA_WORKSPACE to a real path or pass --workspace PATH; nothing was created"
    exit 2
fi
if [[ -z "$ROOT" ]]; then
    err "MOEKA_WORKSPACE (instance root) is empty"
    exit 2
fi
ROOT="$(_normalize_path "$ROOT")"
HOME_NORM="$(_normalize_path "$HOME")"
readonly ROOT
export MOEKA_WORKSPACE="$ROOT"
MOEKA_WORKSPACE_EXPANDED="$ROOT"   # compat name used by export/import

# Instance kind, name and own systemd unit (FR-012).
INSTANCE_KIND="registered"
INSTANCE_NAME="$(basename -- "$ROOT")"
UNIT=""
if [[ "$ROOT" == "$HOME_NORM/.nanobot" ]]; then
    INSTANCE_KIND="default"; INSTANCE_NAME="default"; UNIT="moeka.service"
elif [[ "$(dirname -- "$ROOT")" == "$HOME_NORM" && "$INSTANCE_NAME" == .moeka-* ]]; then
    _n="${INSTANCE_NAME#.moeka-}"
    if [[ "$_n" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]]; then
        INSTANCE_KIND="named"; INSTANCE_NAME="$_n"; UNIT="moeka@${_n}.service"
    fi
fi

# ---------- env loading (FR-003, Q3) -----------------------------------------
_load_env_file() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    info "loading env: $f"
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
    if [[ "${MOEKA_WORKSPACE:-}" != "$ROOT" ]]; then
        warn "$f sets MOEKA_WORKSPACE='${MOEKA_WORKSPACE:-}'; ignored (instance root is $ROOT)"
    fi
    export MOEKA_WORKSPACE="$ROOT"
}

# The repo-level files are for the default instance only (or MOEKA_REPO_ENV=1).
if [[ "$INSTANCE_KIND" == "default" || "${MOEKA_REPO_ENV:-}" == "1" ]]; then
    _load_env_file "${SCRIPT_DIR}/.env"
    _load_env_file "${SCRIPT_DIR}/keys.env"
fi
_load_env_file "${ROOT}/.env"
_load_env_file "${ROOT}/keys.env"

if [[ -n "$CONFIG_OVERRIDE" ]]; then
    export MOEKA_CONFIG="$CONFIG_OVERRIDE"
fi
if [[ -n "${MOEKA_CONFIG:-}" ]]; then
    _cfg="$(_expand_tilde "$MOEKA_CONFIG")"
    if [[ "$_cfg" == *'${'* ]]; then
        err "MOEKA_CONFIG has an unexpanded variable: '$_cfg'"
        exit 2
    fi
    CFG="$(_normalize_path "$_cfg")"
else
    CFG="${ROOT}/config.json"
fi
readonly CFG

# ---------- runtime paths ---------------------------------------------------
PID_FILE="${ROOT}/moeka.pid"
LOCK_FILE="${ROOT}/gateway.lock"
LOG_FILE="${ROOT}/moeka.log"

# ---------- external commands (overridable for tests) ------------------------
_systemctl() {
    local -a cmd
    read -r -a cmd <<< "${MOEKA_SYSTEMCTL:-systemctl}"
    "${cmd[@]}" "$@"
}

_loginctl() {
    local -a cmd
    read -r -a cmd <<< "${MOEKA_LOGINCTL:-loginctl}"
    "${cmd[@]}" "$@"
}

_py() {
    # The instance helper is stdlib-only; -P keeps nanobot/config off sys.path.
    python3 -P "$INSTANCES_PY" "$@"
}

# ---------- venv plumbing ---------------------------------------------------
_ensure_venv() {
    [[ -n "${MOEKA_NANOBOT_BIN:-}" ]] && return 0
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then return 0; fi
    info "creating venv at $VENV_DIR"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV_DIR" >/dev/null
        info "installing moeka (uv pip install -e '.[vec]')"
        uv pip install --python "$VENV_DIR/bin/python" -e ".[vec]" >/dev/null
    else
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
        info "installing moeka (pip install -e '.[vec]')"
        "$VENV_DIR/bin/pip" install --quiet -e ".[vec]"
    fi
    ok "venv ready"
}

_nanobot_bin() {
    if [[ -n "${MOEKA_NANOBOT_BIN:-}" ]]; then
        echo "$MOEKA_NANOBOT_BIN"
        return 0
    fi
    _ensure_venv
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then
        echo "$VENV_DIR/bin/nanobot"
    elif command -v nanobot >/dev/null 2>&1; then
        command -v nanobot
    else
        err "nanobot binary not found; run ./bin/moeka.sh install"
        exit 1
    fi
}

_python_bin() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then echo "$VENV_DIR/bin/python"; else echo python3; fi
}

# ---------- instance detection (/proc only; no process-name matching) --------
_unit_active() {
    [[ -n "$UNIT" ]] || return 1
    _systemctl --user is-active --quiet "$UNIT" >/dev/null 2>&1
}

_pid_from_file() {
    [[ -f "$PID_FILE" ]] || return 1
    local p
    p="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
    [[ "$p" =~ ^[0-9]+$ ]] || return 1
    (( 10#$p > 0 )) || return 1
    printf '%s' "$((10#$p))"
}

_pid_is_ours() {
    # True when /proc/<pid>/cmdline names this instance's config (FR-004b).
    local pid="$1" arg
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        if [[ "$arg" == "$CFG" || "$arg" == "--config=$CFG" ]]; then
            return 0
        fi
    done < "/proc/$pid/cmdline" 2>/dev/null
    return 1
}

_proc_alive() {
    # A zombie (state Z/X) counts as gone.
    local stat
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    stat="${stat##*) }"
    [[ "${stat%% *}" != "Z" && "${stat%% *}" != "X" ]]
}

_cmdline_has() {
    local pid="$1" needle="$2" arg
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        [[ "$arg" == *"$needle"* ]] && return 0
    done < "/proc/$pid/cmdline" 2>/dev/null
    return 1
}

_gateway_lock_held() {
    [[ -f "$LOCK_FILE" ]] || return 1
    if command -v flock >/dev/null 2>&1; then
        if flock -n "$LOCK_FILE" true 2>/dev/null; then return 1; fi
        return 0
    fi
    python3 - "$LOCK_FILE" <<'PY'
import errno, fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)
fcntl.flock(fd, fcntl.LOCK_UN)
sys.exit(1)
PY
}

_live_pid() {
    local pid
    pid="$(_pid_from_file)" || return 1
    _pid_is_ours "$pid" || return 1
    printf '%s' "$pid"
}

_instance_runs() {
    _unit_active && return 0
    _live_pid >/dev/null && return 0
    _gateway_lock_held && return 0
    return 1
}

_uptime_of() {
    # Elapsed time of <pid> from /proc (no procps).
    local pid="$1"
    [[ -r "/proc/$pid/stat" ]] || return 0
    local start_ticks hz up
    start_ticks="$(sed -E 's/^.*\) //' "/proc/$pid/stat" 2>/dev/null | awk '{print $20}')"
    hz="$(getconf CLK_TCK 2>/dev/null || echo 100)"
    up="$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)"
    [[ -n "$start_ticks" ]] || return 0
    local secs=$(( up - start_ticks / hz ))
    (( secs < 0 )) && secs=0
    printf '%dd %02d:%02d:%02d' $((secs/86400)) $((secs%86400/3600)) $((secs%3600/60)) $((secs%60))
}

# ---------- commands --------------------------------------------------------
cmd_install() {
    if [[ -n "${MOEKA_NANOBOT_BIN:-}" ]]; then
        warn "MOEKA_NANOBOT_BIN is set; not creating a venv"
        return 0
    fi
    _ensure_venv
    local py; py="$("$VENV_DIR/bin/python" --version 2>&1)"
    local ver; ver="$("$VENV_DIR/bin/nanobot" --version 2>/dev/null || echo "unknown")"
    ok "install complete"
    printf '  python  : %s\n' "$py"
    printf '  moeka   : %s\n' "$ver"
    printf '  venv    : %s\n' "$VENV_DIR"
}

cmd_version() {
    local bin; bin="$(_nanobot_bin)"
    "$bin" --version 2>/dev/null || echo "unknown"
}

cmd_run() {
    # Run the gateway in the FOREGROUND (systemd ExecStart / `start`'s child).
    local bin; bin="$(_nanobot_bin)"
    [[ -f "$CFG" ]] || { err "missing config: $CFG (run ./bin/moeka.sh new NAME first)"; exit 1; }
    mkdir -p "$ROOT"

    # Exclusive lock for the whole process lifetime (held across exec): a
    # duplicate `run` (e.g. a stale backoff timer) exits 0 and starts nothing.
    exec 9>>"$LOCK_FILE"
    local locked=0
    if command -v flock >/dev/null 2>&1; then
        flock -n 9 && locked=1
    else
        python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null \
            && locked=1
    fi
    if (( ! locked )); then
        warn "another gateway of $ROOT already holds $LOCK_FILE; refusing to start a duplicate"
        exit 0
    fi
    echo "$$" > "$PID_FILE"
    exec "$bin" gateway --config "$CFG" --workspace "$ROOT" "$@"
}

cmd_start() {
    _nanobot_bin >/dev/null
    [[ -f "$CFG" ]] || { err "missing config: $CFG (run ./bin/moeka.sh new NAME first)"; exit 1; }

    # Do not call 'systemctl start' here: `run` is the unit's ExecStart.
    if _unit_active; then
        warn "unit $UNIT is already active; use 'systemctl --user restart $UNIT' to bounce it"
        return 0
    fi
    if _instance_runs; then
        local pid; pid="$(_pid_from_file || echo '?')"
        warn "instance $ROOT is already running (PID $pid); use restart to bounce it"
        return 0
    fi
    if [[ -e "$PID_FILE" ]]; then
        rm -f "$PID_FILE"
    fi

    mkdir -p "$ROOT"
    info "starting gateway in background"
    info "  instance : $ROOT"
    info "  config   : $CFG"
    info "  log      : $LOG_FILE"

    # `run` holds gateway.lock and writes the PID file; the PID survives its exec.
    nohup bash "${SCRIPT_DIR}/bin/moeka.sh" --workspace "$ROOT" --config "$CFG" run "$@" \
        </dev/null >>"$LOG_FILE" 2>&1 &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" > "$PID_FILE"
    # Wait (bounded) until the wrapper has exec'd the gateway.
    local i=0
    while (( i < 200 )); do
        _proc_alive "$pid" || break
        if _pid_is_ours "$pid" && ! _cmdline_has "$pid" "bin/moeka.sh"; then break; fi
        sleep 0.05
        i=$((i + 1))
    done
    if _pid_is_ours "$pid"; then
        ok "moeka started (PID $pid)"
    else
        warn "gateway process $pid exited early; see $LOG_FILE"
    fi
}

_stop_timeout_ticks() {
    # MOEKA_STOP_TIMEOUT_S (default 10, fractions allowed) in 0.1 s ticks.
    local t="${MOEKA_STOP_TIMEOUT_S:-10}"
    awk -v t="$t" 'BEGIN { v = t + 0; if (v < 0) v = 0; printf "%d", v * 10 + 0.5 }'
}

cmd_stop() {
    local stopped=0

    # (a) this instance's own unit, when active.
    if _unit_active; then
        info "stopping unit $UNIT"
        _systemctl --user stop "$UNIT" || warn "systemctl --user stop $UNIT failed"
        stopped=1
    fi

    # (b) the PID-file process, only when /proc/<pid>/cmdline names our config;
    # (c) otherwise the PID file is stale: signal nothing, delete it.
    if [[ -e "$PID_FILE" ]]; then
        local pid=""
        if pid="$(_pid_from_file)" && _pid_is_ours "$pid"; then
            info "stopping gateway (PID $pid)..."
            kill -TERM "$pid" 2>/dev/null || true
            local ticks; ticks="$(_stop_timeout_ticks)"
            local i=0
            while _pid_is_ours "$pid" && (( i < ticks )); do
                sleep 0.1
                i=$((i + 1))
            done
            if _pid_is_ours "$pid"; then
                warn "graceful stop timed out after ${MOEKA_STOP_TIMEOUT_S:-10}s; sending SIGKILL"
                kill -KILL "$pid" 2>/dev/null || true
                i=0
                while _pid_is_ours "$pid" && (( i < 50 )); do
                    sleep 0.1
                    i=$((i + 1))
                done
            fi
            stopped=1
        else
            warn "stale PID file $PID_FILE (${pid:-unreadable}); not this instance's gateway, removing"
        fi
        rm -f "$PID_FILE"
    fi

    if (( stopped )); then
        ok "moeka stopped ($ROOT)"
    else
        warn "no running gateway found for $ROOT"
    fi
    return 0
}

cmd_restart() {
    cmd_stop || true
    cmd_start "$@"
}

cmd_status() {
    local json=0
    while (( $# > 0 )); do
        case "$1" in
            --json) json=1; shift ;;
            *) err "unknown status flag: $1"; exit 2 ;;
        esac
    done
    local rc=0
    if (( json )); then
        _py status --root "$ROOT" --config "$CFG" --json || rc=$?
        return "$rc"
    fi
    _py status --root "$ROOT" --config "$CFG" || rc=$?
    printf 'log file     : %s\n' "$LOG_FILE"
    if _unit_active; then
        printf '\n'
        _systemctl --user status "$UNIT" --no-pager 2>/dev/null || true
    else
        local pid
        if pid="$(_live_pid)"; then
            printf 'uptime       : %s\n' "$(_uptime_of "$pid")"
        elif [[ -e "$PID_FILE" ]]; then
            warn "stale PID file $PID_FILE"
        fi
    fi
    if [[ -f "$LOG_FILE" ]]; then
        printf '\n%s--- last 5 log lines ---%s\n' "$_C_DIM" "$_C_RESET"
        tail -n 5 "$LOG_FILE" || true
    fi
    return "$rc"
}

cmd_list() {
    local json=0
    while (( $# > 0 )); do
        case "$1" in
            --json) json=1; shift ;;
            *) err "unknown list flag: $1"; exit 2 ;;
        esac
    done
    if (( json )); then _py list --json; else _py list; fi
}

cmd_logs() {
    local follow=0 lines=100
    local extra_args=()
    while (( $# > 0 )); do
        case "$1" in
            -f) follow=1; shift ;;
            -n) lines="${2:?-n requires a number}"; shift 2 ;;
            -n*) lines="${1#-n}"; shift ;;
            *) extra_args+=("$1"); shift ;;
        esac
    done

    if _unit_active; then
        local jargs=("--user" "-u" "$UNIT")
        if (( follow )); then jargs+=("-f"); else jargs+=("-n" "$lines"); fi
        journalctl "${jargs[@]}" "${extra_args[@]+"${extra_args[@]}"}"
    elif [[ -f "$LOG_FILE" ]]; then
        printf '%s[%s]%s\n' "$_C_DIM" "$LOG_FILE" "$_C_RESET" >&2
        if (( follow )); then
            tail -f "$LOG_FILE"
        else
            tail -n "$lines" "$LOG_FILE"
        fi
    else
        warn "no log source found (unit not active, no log file at $LOG_FILE)"
    fi
}

cmd_shell() {
    _ensure_venv
    info "entering moeka venv shell"
    exec "$SHELL" --rcfile <(echo "source $VENV_DIR/bin/activate; PS1='(moeka) \$ '")
}

cmd_exec() {
    local bin; bin="$(_nanobot_bin)"
    exec "$bin" "$@"
}

cmd_enable() {
    case "$INSTANCE_KIND" in
        default)
            _ensure_venv
            bash "${SCRIPT_DIR}/scripts/install-service.sh" "$@"
            ;;
        named)
            bash "${SCRIPT_DIR}/scripts/install-service.sh" "$INSTANCE_NAME" "$@"
            ;;
        *)
            err "instance $ROOT is registered at a custom path and has no systemd unit"
            exit 1
            ;;
    esac
}

cmd_disable() {
    case "$INSTANCE_KIND" in
        named)
            info "disabling $UNIT"
            _systemctl --user disable --now "$UNIT" || true
            ok "$UNIT disabled"
            return 0
            ;;
        default) ;;
        *)
            err "instance $ROOT is registered at a custom path and has no systemd unit"
            exit 1
            ;;
    esac
    # Default instance: today's behaviour (stop, disable, remove moeka.service).
    local state
    state="$(_systemctl --user is-active moeka 2>/dev/null || true)"
    if [[ "$state" == "active" || "$state" == "activating" ]]; then
        info "stopping moeka service..."
        _systemctl --user stop moeka || true
    fi
    _systemctl --user disable moeka 2>/dev/null || true
    local unit_file="$HOME/.config/systemd/user/moeka.service"
    if [[ -f "$unit_file" ]]; then
        rm -f "$unit_file"
        _systemctl --user daemon-reload || true
        ok "moeka service disabled and unit file removed"
    else
        ok "moeka service disabled"
    fi
}

cmd_doctor() {
    printf '%s=== Runtime ===%s\n' "$_C_BLUE" "$_C_RESET"
    command -v python3 >/dev/null 2>&1 \
        && printf 'python3       : %s\n' "$(python3 --version)" \
        || printf 'python3       : %snot installed%s\n' "$_C_RED" "$_C_RESET"
    command -v uv >/dev/null 2>&1 \
        && printf 'uv            : %s\n' "$(uv --version)" \
        || printf 'uv            : %snot installed%s (recommended: https://docs.astral.sh/uv/)\n' "$_C_YELLOW" "$_C_RESET"
    if [[ -n "${MOEKA_NANOBOT_BIN:-}" ]]; then
        printf 'nanobot bin   : %s (MOEKA_NANOBOT_BIN)\n' "$MOEKA_NANOBOT_BIN"
    elif [[ -x "$VENV_DIR/bin/nanobot" ]]; then
        local venv_py; venv_py="$("$VENV_DIR/bin/python" --version 2>&1)"
        local moeka_ver; moeka_ver="$("$VENV_DIR/bin/python" -c "import importlib.metadata; print(importlib.metadata.version('moeka'))" 2>/dev/null || echo "unknown")"
        printf 'venv python   : %s%s%s\n' "$_C_GREEN" "$venv_py" "$_C_RESET"
        printf 'moeka version : %s%s%s\n' "$_C_GREEN" "$moeka_ver" "$_C_RESET"
        printf 'nanobot bin   : %s\n' "$VENV_DIR/bin/nanobot"
    else
        printf 'venv nanobot  : %snot built%s (run ./bin/moeka.sh install)\n' "$_C_RED" "$_C_RESET"
    fi

    printf '\n%s=== Instance ===%s\n' "$_C_BLUE" "$_C_RESET"
    printf 'instance      : %s (%s)\n' "$INSTANCE_NAME" "$INSTANCE_KIND"
    printf 'workspace     : %s\n' "$ROOT"
    printf 'unit          : %s\n' "${UNIT:-none}"
    if [[ -d "$ROOT" ]]; then
        local disk_usage; disk_usage="$(du -sh "$ROOT" 2>/dev/null | cut -f1)"
        printf 'disk usage    : %s\n' "${disk_usage:-unknown}"
    fi
    [[ -f "$CFG" ]] \
        && printf 'config.json   : %spresent%s (%s)\n' "$_C_GREEN" "$_C_RESET" "$CFG" \
        || printf 'config.json   : %smissing%s (%s)\n' "$_C_YELLOW" "$_C_RESET" "$CFG"
    [[ -f "${ROOT}/keys.env" ]] \
        && printf 'keys.env      : %spresent%s (%s)\n' "$_C_GREEN" "$_C_RESET" "${ROOT}/keys.env" \
        || printf 'keys.env      : %smissing%s (%s)\n' "$_C_YELLOW" "$_C_RESET" "${ROOT}/keys.env"
    if [[ "$INSTANCE_KIND" == "default" || "${MOEKA_REPO_ENV:-}" == "1" ]]; then
        [[ -f "${SCRIPT_DIR}/keys.env" ]] \
            && printf 'repo keys.env : %spresent (loaded)%s\n' "$_C_GREEN" "$_C_RESET" \
            || printf 'repo keys.env : not present\n'
    fi

    if [[ -f "$CFG" ]]; then
        printf '\n%s=== Config ===%s\n' "$_C_BLUE" "$_C_RESET"
        python3 - "$CFG" <<'PYEOF' || true
import json, sys
cfg_path = sys.argv[1]
try:
    c = json.load(open(cfg_path))
    print(f"gateway port  : {c.get('gateway', {}).get('port', 18790)}")
    print(f"api port      : {c.get('api', {}).get('port', 8900)}")
    ws = c.get('channels', {}).get('websocket', {}) or {}
    sock = ws.get('unixSocketPath') or ws.get('unix_socket_path')
    print(f"websocket     : {sock or ws.get('port', 8765)}")
    d = c.get('agents', {}).get('defaults', {})
    print(f"model         : {d.get('model', 'unknown')}")
    print(f"provider      : {d.get('provider', 'unknown')}")
    channels = c.get('channels', {})
    enabled = [k for k, v in channels.items() if isinstance(v, dict) and v.get('enabled')]
    disabled = [k for k, v in channels.items() if isinstance(v, dict) and not v.get('enabled')]
    if enabled:
        print(f"channels on   : {', '.join(enabled)}")
    if disabled:
        print(f"channels off  : {', '.join(disabled)}")
except Exception as e:
    print(f'config parse error: {e}', file=sys.stderr)
PYEOF
    fi

    printf '\n%s=== API Keys ===%s\n' "$_C_BLUE" "$_C_RESET"
    local key_vars=(TELEGRAM_TOKEN DISCORD_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GROQ_API_KEY)
    for v in "${key_vars[@]}"; do
        if [[ -n "${!v:-}" ]]; then
            printf '%-22s: %sset%s\n' "$v" "$_C_GREEN" "$_C_RESET"
        else
            printf '%-22s: %snot set%s\n' "$v" "$_C_DIM" "$_C_RESET"
        fi
    done

    printf '\n%s=== Service ===%s\n' "$_C_BLUE" "$_C_RESET"
    if [[ -n "$UNIT" ]]; then
        if _systemctl --user is-enabled --quiet "$UNIT" >/dev/null 2>&1; then
            local svc_state; svc_state="$(_systemctl --user is-active "$UNIT" 2>/dev/null; true)"
            printf 'systemd       : %senabled%s (%s, %s)\n' "$_C_GREEN" "$_C_RESET" "$UNIT" "${svc_state:-unknown}"
        else
            printf 'systemd       : %s not enabled (run ./bin/moeka.sh enable)\n' "$UNIT"
        fi
        local linger_state; linger_state="$(_loginctl show-user "${USER:-$(id -un)}" 2>/dev/null | sed -n 's/^Linger=//p' || true)"
        if [[ "$linger_state" == "yes" ]]; then
            printf 'linger        : %senabled%s (autostart on boot OK)\n' "$_C_GREEN" "$_C_RESET"
        else
            printf 'linger        : %sdisabled%s (run: sudo loginctl enable-linger %s)\n' "$_C_YELLOW" "$_C_RESET" "${USER:-$(id -un)}"
        fi
    else
        printf 'systemd       : none (registered instance at a custom path)\n'
    fi
    local pid
    if pid="$(_live_pid)"; then
        printf 'process       : %srunning%s (PID %s, up %s)\n' "$_C_GREEN" "$_C_RESET" "$pid" "$(_uptime_of "$pid")"
    elif _unit_active; then
        printf 'process       : %srunning%s (unit %s)\n' "$_C_GREEN" "$_C_RESET" "$UNIT"
    elif _gateway_lock_held; then
        printf 'process       : %srunning%s (holds %s)\n' "$_C_GREEN" "$_C_RESET" "$LOCK_FILE"
    else
        [[ -e "$PID_FILE" ]] && printf 'process       : %sstale PID file%s\n' "$_C_YELLOW" "$_C_RESET"
        printf 'process       : not running\n'
    fi
}

# ---------- portability commands -------------------------------------------

# Items that are ALWAYS excluded from an export: secrets, runtime state,
# logs, bulky on-disk caches, and editor/IDE droppings.
_export_default_excludes() {
    cat <<'EOF'
keys.env
.env
moeka.pid
gateway.lock
moeka.log
moeka.log.*
moeka.*.log
moeka.*.log.gz
config.json.bak.*
tool-results
bg-shell
run
.instance.lock
.instance.json
EOF
}

cmd_export() {
    local out=""
    local with_sessions=0
    local with_media=0
    local anonymize=0
    while (( $# > 0 )); do
        case "$1" in
            --out) out="$2"; shift 2 ;;
            --with-sessions) with_sessions=1; shift ;;
            --with-media)    with_media=1;    shift ;;
            --anonymize)     anonymize=1;     shift ;;
            *) err "unknown export flag: $1"; exit 2 ;;
        esac
    done

    [[ -d "$MOEKA_WORKSPACE_EXPANDED" ]] || { err "workspace not found: $MOEKA_WORKSPACE_EXPANDED"; exit 1; }

    if [[ -z "$out" ]]; then
        local stamp; stamp="$(date -u +%Y%m%d-%H%M%S)"
        out="${PWD}/moeka-export-$(hostname -s 2>/dev/null || hostname)-${stamp}.tar.gz"
    fi

    local tmpdir; tmpdir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmpdir'" RETURN

    local excludes_file="$tmpdir/excludes"
    _export_default_excludes > "$excludes_file"
    (( with_sessions )) || echo "sessions" >> "$excludes_file"
    (( with_media ))    || echo "media"    >> "$excludes_file"

    local stage="$tmpdir/workspace"
    mkdir -p "$stage"
    info "staging workspace from $MOEKA_WORKSPACE_EXPANDED"
    # Use tar to copy with excludes (rsync may not be installed everywhere).
    tar -C "$MOEKA_WORKSPACE_EXPANDED" \
        --exclude-from="$excludes_file" \
        --exclude='memory/*.db-shm' --exclude='memory/*.db-wal' \
        -cf - . | tar -C "$stage" -xf -

    if (( anonymize )); then
        info "anonymizing identity files"
        cat > "$stage/USER.md" <<'EOF'
# User

Replace with the new user's profile.
EOF
        if [[ -f "$stage/config.json" ]]; then
            "$(_python_bin)" - "$stage/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
for name, ch in (c.get("channels") or {}).items():
    if isinstance(ch, dict) and "allowFrom" in ch:
        ch["allowFrom"] = []
        if "enabled" in ch:
            ch["enabled"] = False
open(p, "w").write(json.dumps(c, indent=2) + "\n")
PY
        fi
    fi

    info "writing archive: $out"
    tar -C "$stage" -czf "$out" .
    local size; size="$(du -h "$out" | cut -f1)"
    local count; count="$(tar -tzf "$out" | wc -l)"
    ok "export complete"
    printf '  archive : %s\n' "$out"
    printf '  size    : %s\n' "$size"
    printf '  entries : %s\n' "$count"
    (( with_sessions )) && printf '  scope   : +sessions\n'
    (( with_media ))    && printf '  scope   : +media\n'
    (( anonymize ))     && printf '  scope   : anonymized\n'
}

cmd_import() {
    local force=0
    local archive=""
    while (( $# > 0 )); do
        case "$1" in
            --force) force=1; shift ;;
            -h|--help) echo "usage: moeka.sh import FILE [--force]"; return 0 ;;
            *) archive="$1"; shift ;;
        esac
    done
    [[ -n "$archive" ]] || { err "usage: moeka.sh import FILE [--force]"; exit 2; }
    [[ -f "$archive" ]] || { err "archive not found: $archive"; exit 1; }

    local ws="$MOEKA_WORKSPACE_EXPANDED"
    if [[ -d "$ws" && -n "$(ls -A "$ws" 2>/dev/null)" ]] && (( !force )); then
        err "workspace not empty: $ws (use --force to overwrite)"
        exit 1
    fi
    mkdir -p "$ws"
    info "extracting $archive -> $ws"
    tar -C "$ws" -xzf "$archive"
    ok "import complete"

    # Warn about missing env vars referenced in config.json.
    local cfg="$ws/config.json"
    if [[ -f "$cfg" ]]; then
        local missing
        missing="$(python3 - "$cfg" <<'PY' || true
import json, os, re, sys
c = open(sys.argv[1]).read()
keys = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", c)))
missing = [k for k in keys if not os.environ.get(k)]
print(" ".join(missing))
PY
)"
        if [[ -n "$missing" ]]; then
            warn "config references env vars not currently set: $missing"
            warn "add them to keys.env before starting, or run ./bin/moeka.sh telegram-pair / edit keys.env"
        fi
    fi

    cat <<EOF

Next steps:
  1. Edit keys.env to set provider keys and bot tokens.
  2. ./bin/moeka.sh telegram-pair    # if using Telegram (captures token + user id)
  3. ./bin/moeka.sh start            # or ./bin/moeka.sh enable for boot autostart
EOF
}

cmd_new() {
    local name="${1:-}"
    [[ -n "$name" ]] || { err "usage: moeka.sh new NAME [--workspace PATH] [--port-base N] [--ws-tcp]"; exit 2; }
    shift || true
    local args=("$name" --template "${SCRIPT_DIR}/templates/workspace"
                --keys-example "${SCRIPT_DIR}/keys.env.example")
    while (( $# > 0 )); do
        case "$1" in
            --port-base)
                [[ $# -lt 2 ]] && { err "--port-base requires a number"; exit 2; }
                args+=(--port-base "$2"); shift 2 ;;
            --port-base=*) args+=(--port-base "${1#--port-base=}"); shift ;;
            --ws-tcp) args+=(--ws-tcp); shift ;;
            *) err "unknown new flag: $1"; exit 2 ;;
        esac
    done
    # The top-level parser consumed --workspace (validated and made absolute);
    # without it the target is ~/.moeka-NAME.
    if (( WORKSPACE_GIVEN )); then
        args+=(--workspace "$ROOT")
    fi
    local rc=0
    _py new "${args[@]}" || rc=$?
    if (( rc != 0 )); then
        exit "$rc"
    fi
    local target="$HOME/.moeka-$name"
    (( WORKSPACE_GIVEN )) && target="$ROOT"
    ok "instance ready: $target"
    cat >&2 <<EOF

To use this instance:
  ./bin/moeka.sh --workspace $target telegram-pair    # wire up Telegram (optional)
  ./bin/moeka.sh --workspace $target start
  ./bin/moeka.sh --workspace $target enable           # boot autostart (named instances)

Edit $target/SOUL.md to define this agent's personality.
Edit $target/USER.md to describe the user.
Secrets go in $target/keys.env (mode 0600).
EOF
}

cmd_telegram_pair() {
    [[ -f "$CFG" ]] || { err "config.json not found: $CFG (run ./bin/moeka.sh new or onboard first)"; exit 1; }
    local keys="${ROOT}/keys.env"
    mkdir -p "$ROOT"
    if [[ ! -e "$keys" ]]; then
        (umask 077; : > "$keys")
    fi
    chmod 600 "$keys" 2>/dev/null || true
    info "pairing Telegram bot"
    info "  config : $CFG"
    info "  keys   : $keys"
    local rc=0
    "$(_python_bin)" "${SCRIPT_DIR}/scripts/telegram_pair.py" "$keys" "$CFG" "$@" || rc=$?
    if (( rc == 0 )); then
        ok "telegram paired — restart this instance to apply: ./bin/moeka.sh --workspace $ROOT restart"
    fi
    return $rc
}

cmd_help() {
    sed -n '2,37p' "${SCRIPT_DIR}/bin/moeka.sh" | sed 's/^# \{0,1\}//'
}

case "$CMD" in
    start)          cmd_start "$@" ;;
    run)            cmd_run "$@" ;;
    stop)           cmd_stop ;;
    restart)        cmd_restart "$@" ;;
    status)         cmd_status "$@" ;;
    list)           cmd_list "$@" ;;
    logs)           cmd_logs "$@" ;;
    shell)          cmd_shell ;;
    exec)           cmd_exec "$@" ;;
    install)        cmd_install ;;
    version)        cmd_version ;;
    doctor)         cmd_doctor ;;
    enable)         cmd_enable "$@" ;;
    disable)        cmd_disable ;;
    export)         cmd_export "$@" ;;
    import)         cmd_import "$@" ;;
    new)            cmd_new "$@" ;;
    telegram-pair)  cmd_telegram_pair "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
        err "unknown command: $CMD"
        cmd_help >&2
        exit 2 ;;
esac
