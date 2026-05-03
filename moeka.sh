#!/usr/bin/env bash
# Moeka — nanobot for server management.
#
# Usage:
#   ./moeka.sh start            # start the gateway in the background (terminal-safe)
#   ./moeka.sh stop             # stop the running instance
#   ./moeka.sh restart          # stop + start
#   ./moeka.sh status           # show service/process status
#   ./moeka.sh logs [-f]        # show (or tail -f) logs
#   ./moeka.sh shell            # drop into the moeka venv
#   ./moeka.sh exec -- CMD ...  # run a command inside the venv
#   ./moeka.sh install          # install Python deps into .venv
#   ./moeka.sh doctor           # sanity check the environment
#   ./moeka.sh enable           # install + enable systemd user service
#   ./moeka.sh disable          # stop + disable systemd user service
#
# Flags (anywhere on the command line):
#   --config PATH       override the config file path
#   --workspace PATH    override MOEKA_WORKSPACE (instance dir, default ~/.nanobot)

set -euo pipefail

# ---------- paths & constants -----------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/.venv"

# ---------- tiny logging helpers --------------------------------------------
_is_tty() { [ -t 1 ]; }
if _is_tty; then
    _C_BLUE=$'\033[34m'; _C_GREEN=$'\033[32m'; _C_YELLOW=$'\033[33m'
    _C_RED=$'\033[31m'; _C_DIM=$'\033[2m'; _C_RESET=$'\033[0m'
else
    _C_BLUE=""; _C_GREEN=""; _C_YELLOW=""; _C_RED=""; _C_DIM=""; _C_RESET=""
fi
info() { printf '%s[moeka]%s %s\n' "$_C_BLUE" "$_C_RESET" "$*"; }
ok()   { printf '%s[moeka]%s %s\n' "$_C_GREEN" "$_C_RESET" "$*"; }
warn() { printf '%s[moeka]%s %s\n' "$_C_YELLOW" "$_C_RESET" "$*" >&2; }
err()  { printf '%s[moeka]%s %s\n' "$_C_RED" "$_C_RESET" "$*" >&2; }

# ---------- argv parsing ----------------------------------------------------
CONFIG_OVERRIDE=""
WORKSPACE_OVERRIDE=""
POSITIONAL=()

while (( $# > 0 )); do
    case "$1" in
        --config)
            [[ $# -lt 2 ]] && { err "--config requires a path"; exit 2; }
            CONFIG_OVERRIDE="$2"; shift 2 ;;
        --workspace)
            [[ $# -lt 2 ]] && { err "--workspace requires a path"; exit 2; }
            WORKSPACE_OVERRIDE="$2"; shift 2 ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"
CMD="${1:-help}"
[[ $# -gt 0 ]] && shift || true

# ---------- env loading -----------------------------------------------------
_load_env_file() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    info "loading env: $f"
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
}

if [[ -n "$WORKSPACE_OVERRIDE" ]]; then
    export MOEKA_WORKSPACE="$WORKSPACE_OVERRIDE"
fi
: "${MOEKA_WORKSPACE:=$HOME/.nanobot}"
export MOEKA_WORKSPACE
MOEKA_WORKSPACE_EXPANDED="${MOEKA_WORKSPACE/#\~/$HOME}"

_load_env_file "${SCRIPT_DIR}/.env"
_load_env_file "${SCRIPT_DIR}/keys.env"
_load_env_file "${MOEKA_WORKSPACE_EXPANDED}/.env"
_load_env_file "${MOEKA_WORKSPACE_EXPANDED}/keys.env"

if [[ -n "$CONFIG_OVERRIDE" ]]; then
    export MOEKA_CONFIG="$CONFIG_OVERRIDE"
fi

# ---------- venv plumbing ---------------------------------------------------
_ensure_venv() {
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then return 0; fi
    info "creating venv at $VENV_DIR"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV_DIR" >/dev/null
        info "installing moeka (uv pip install -e .)"
        uv pip install --python "$VENV_DIR/bin/python" -e . >/dev/null
    else
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
        info "installing moeka (pip install -e .)"
        "$VENV_DIR/bin/pip" install --quiet -e .
    fi
    ok "venv ready"
}

_nanobot_bin() {
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then
        echo "$VENV_DIR/bin/nanobot"
    elif command -v nanobot >/dev/null 2>&1; then
        command -v nanobot
    else
        err "nanobot binary not found; run ./moeka.sh install"
        exit 1
    fi
}

# ---------- runtime paths ---------------------------------------------------
PID_FILE="${MOEKA_WORKSPACE_EXPANDED}/moeka.pid"
LOG_FILE="${MOEKA_WORKSPACE_EXPANDED}/moeka.log"

# ---------- commands --------------------------------------------------------
cmd_install() {
    _ensure_venv
}

_is_systemd_active() {
    systemctl --user is-active --quiet moeka 2>/dev/null || \
    systemctl --user is-active --quiet nanobot 2>/dev/null
}

cmd_start() {
    _ensure_venv
    local bin; bin="$(_nanobot_bin)"
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"

    # If the systemd unit is managing moeka, delegate to it.
    if systemctl --user is-enabled --quiet moeka 2>/dev/null; then
        info "systemd unit is enabled — use './moeka.sh enable' to (re)start, or 'systemctl --user start moeka'"
        systemctl --user start moeka
        ok "moeka started via systemd"
        return 0
    fi

    # Check for a stale or live PID file.
    if [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            warn "moeka is already running (PID $pid) — use restart to bounce it"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    mkdir -p "$MOEKA_WORKSPACE_EXPANDED"
    info "starting gateway in background"
    info "  config : $cfg"
    info "  log    : $LOG_FILE"

    # nohup detaches from SIGHUP; disown removes it from the shell job table so
    # closing the terminal won't signal the process.
    nohup "$bin" gateway --config "$cfg" "$@" </dev/null >>"$LOG_FILE" 2>&1 &
    local pid=$!
    disown "$pid"
    echo "$pid" > "$PID_FILE"
    ok "moeka started (PID $pid)"
}

cmd_stop() {
    # Prefer systemd when the unit is active.
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user stop moeka
        ok "moeka stopped (systemd)"
        return 0
    fi
    if systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user stop nanobot
        ok "moeka stopped (systemd)"
        return 0
    fi

    # Fall back to PID file.
    if [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            info "stopping moeka (PID $pid)..."
            kill "$pid"
            local i=0
            while kill -0 "$pid" 2>/dev/null && (( i < 20 )); do
                sleep 0.5
                (( i++ ))
            done
            if kill -0 "$pid" 2>/dev/null; then
                warn "graceful stop timed out — sending SIGKILL"
                kill -9 "$pid" 2>/dev/null || true
            fi
            rm -f "$PID_FILE"
            ok "moeka stopped"
            return 0
        else
            warn "stale PID file removed"
            rm -f "$PID_FILE"
        fi
    fi

    # Last resort: find by name.
    if pkill -f "nanobot gateway" 2>/dev/null; then
        ok "moeka stopped (pkill)"
    else
        warn "no running gateway found"
    fi
}

cmd_restart() {
    cmd_stop || true
    cmd_start "$@"
}

cmd_status() {
    printf 'workspace    : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    printf 'config file  : %s\n' "${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    printf 'log file     : %s\n' "$LOG_FILE"
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user status moeka --no-pager || true
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user status nanobot --no-pager || true
    elif [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            ok "running (PID $pid, background)"
        else
            warn "stale PID file — moeka is not running"
        fi
    else
        echo "no gateway process running"
    fi
}

cmd_logs() {
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        journalctl --user -u moeka "$@"
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        journalctl --user -u nanobot "$@"
    elif [[ -f "$LOG_FILE" ]]; then
        if [[ "${1:-}" == "-f" ]]; then
            tail -f "$LOG_FILE"
        else
            tail -n 100 "$LOG_FILE"
        fi
    else
        warn "no log source found (systemd not active, no log file at $LOG_FILE)"
    fi
}

cmd_shell() {
    _ensure_venv
    info "entering moeka venv shell"
    exec "$SHELL" --rcfile <(echo "source $VENV_DIR/bin/activate; PS1='(moeka) \$ '")
}

cmd_exec() {
    _ensure_venv
    exec "$VENV_DIR/bin/nanobot" "$@"
}

cmd_enable() {
    _ensure_venv
    bash "${SCRIPT_DIR}/install-service.sh"
}

cmd_disable() {
    # Stop if running (active) or starting (activating)
    local state
    state="$(systemctl --user is-active moeka 2>/dev/null || true)"
    if [[ "$state" == "active" || "$state" == "activating" ]]; then
        info "stopping moeka service..."
        systemctl --user stop moeka || true
    fi
    # Disable and remove the unit file so it won't auto-start on next login
    systemctl --user disable moeka 2>/dev/null || true
    local unit_file="$HOME/.config/systemd/user/moeka.service"
    if [[ -f "$unit_file" ]]; then
        rm -f "$unit_file"
        systemctl --user daemon-reload
        ok "moeka service disabled and unit file removed"
    else
        ok "moeka service disabled"
    fi
}

cmd_doctor() {
    command -v python3 >/dev/null 2>&1 \
        && printf 'python3       : %s\n' "$(python3 --version)" \
        || printf 'python3       : not installed\n'
    command -v uv >/dev/null 2>&1 \
        && printf 'uv            : %s\n' "$(uv --version)" \
        || printf 'uv            : not installed (recommended: https://docs.astral.sh/uv/)\n'
    [[ -x "$VENV_DIR/bin/nanobot" ]] \
        && printf 'venv nanobot  : %s\n' "$VENV_DIR/bin/nanobot" \
        || printf 'venv nanobot  : not built (run ./moeka.sh install)\n'
    printf 'workspace     : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    [[ -f "${MOEKA_WORKSPACE_EXPANDED}/config.json" ]] \
        && printf 'config.json   : present\n' \
        || printf 'config.json   : %smissing%s\n' "$_C_YELLOW" "$_C_RESET"
    [[ -f "${SCRIPT_DIR}/keys.env" ]] \
        && printf 'keys.env      : present (repo)\n' \
        || printf 'keys.env      : missing (see keys.env.example)\n'

    if [[ -f "${MOEKA_WORKSPACE_EXPANDED}/config.json" ]]; then
        if grep -q '"allowSudo"\s*:\s*true' "${MOEKA_WORKSPACE_EXPANDED}/config.json" 2>/dev/null; then
            printf 'allow_sudo    : %senabled%s in config (host sudo policy is managed outside moeka.sh)\n' "$_C_GREEN" "$_C_RESET"
        else
            printf 'allow_sudo    : disabled in config\n'
        fi
    fi

    # Systemd status
    if systemctl --user is-enabled --quiet moeka 2>/dev/null; then
        printf 'systemd       : %senabled%s\n' "$_C_GREEN" "$_C_RESET"
    else
        printf 'systemd       : not enabled (run ./moeka.sh enable)\n'
    fi
}

cmd_help() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

case "$CMD" in
    start)       cmd_start "$@" ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart "$@" ;;
    status)      cmd_status ;;
    logs)        cmd_logs "$@" ;;
    shell)       cmd_shell ;;
    exec)        cmd_exec "$@" ;;
    install)     cmd_install ;;
    doctor)      cmd_doctor ;;
    enable)      cmd_enable ;;
    disable)     cmd_disable ;;
    help|-h|--help) cmd_help ;;
    *)
        err "unknown command: $CMD"
        cmd_help
        exit 2 ;;
esac
