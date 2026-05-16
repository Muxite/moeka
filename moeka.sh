#!/usr/bin/env bash
# Moeka — nanobot for server management.
#
# Usage:
#   ./moeka.sh start            # start the gateway in the background (terminal-safe)
#   ./moeka.sh stop             # stop the running instance
#   ./moeka.sh restart          # stop + start
#   ./moeka.sh status           # show service/process status, port, channels, recent logs
#   ./moeka.sh logs [-f] [-n N] # show last N lines (default 100), or tail -f
#   ./moeka.sh shell            # drop into the moeka venv
#   ./moeka.sh exec -- CMD ...  # run a command inside the venv
#   ./moeka.sh install          # install Python deps into .venv, show version
#   ./moeka.sh version          # print installed moeka version
#   ./moeka.sh doctor           # sanity check: runtime, config, api keys, service state
#   ./moeka.sh enable           # install + enable systemd user service
#   ./moeka.sh disable          # stop + disable systemd user service
#   ./moeka.sh export [--out F] # bundle workspace into a portable archive
#   ./moeka.sh import FILE      # extract a workspace archive into MOEKA_WORKSPACE
#   ./moeka.sh new NAME         # scaffold a fresh-identity workspace at ~/.moeka-NAME
#   ./moeka.sh telegram-pair    # pair a Telegram bot token + auto-capture user id
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
    local py; py="$("$VENV_DIR/bin/python" --version 2>&1)"
    local ver; ver="$("$VENV_DIR/bin/nanobot" --version 2>/dev/null || echo "unknown")"
    ok "install complete"
    printf '  python  : %s\n' "$py"
    printf '  moeka   : %s\n' "$ver"
    printf '  venv    : %s\n' "$VENV_DIR"
}

cmd_version() {
    _ensure_venv
    "$VENV_DIR/bin/nanobot" --version 2>/dev/null || \
        "$VENV_DIR/bin/python" -c "import importlib.metadata; print(importlib.metadata.version('moeka'))" 2>/dev/null || \
        echo "unknown"
}

_is_systemd_active() {
    systemctl --user is-active --quiet moeka 2>/dev/null || \
    systemctl --user is-active --quiet nanobot 2>/dev/null
}

cmd_run() {
    # Run the gateway in the FOREGROUND — used by systemd ExecStart only.
    # Do not call this directly; use 'start' for interactive use.
    _ensure_venv
    local bin; bin="$(_nanobot_bin)"
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    mkdir -p "$MOEKA_WORKSPACE_EXPANDED"

    # Acquire an exclusive lock so that duplicate starts (e.g. a stale
    # backoff timer firing after an explicit restart) exit cleanly instead
    # of running a second gateway instance.  The lock is held across exec
    # so it stays alive for the full lifetime of the nanobot process.
    local lock_file="${MOEKA_WORKSPACE_EXPANDED}/gateway.lock"
    exec 9>"${lock_file}"
    if ! flock -n 9; then
        warn "another gateway instance already holds the lock; refusing to start a duplicate"
        exit 0
    fi

    exec "$bin" gateway --config "$cfg"
}

cmd_start() {
    _ensure_venv
    local bin; bin="$(_nanobot_bin)"
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"

    # If the systemd unit is active, it is already running — nothing to do.
    # Don't call 'systemctl start moeka' from within ExecStart — that creates
    # an activation loop.  Users who want to (re)start via systemd should run
    # 'systemctl --user restart moeka' themselves.
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        warn "systemd unit is already active — use 'systemctl --user restart moeka' to bounce it"
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
    local stopped=0

    # Prefer systemd when the unit is active.
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user stop moeka
        ok "moeka stopped (systemd)"
        stopped=1
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user stop nanobot
        ok "moeka stopped (systemd)"
        stopped=1
    fi

    # Clean up PID-file process (may be an orphan separate from systemd).
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
            (( stopped++ )) || true
        fi
        rm -f "$PID_FILE"
    fi

    # Always sweep for any remaining nanobot gateway processes (orphans survive
    # both systemd stop and PID-file stops when processes were started outside
    # systemd supervision).
    if pkill -f "nanobot gateway" 2>/dev/null; then
        (( stopped++ )) || true
    fi

    if (( stopped > 0 )); then
        ok "moeka stopped"
    else
        warn "no running gateway found"
    fi
}

cmd_restart() {
    cmd_stop || true
    cmd_start "$@"
}

_status_config_info() {
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    [[ -f "$cfg" ]] || return 0
    local port channels
    port="$("$VENV_DIR/bin/python" -c "
import json, sys
try:
    c = json.load(open('$cfg'))
    print(c.get('api', {}).get('port', 8900))
except: pass
" 2>/dev/null)"
    channels="$("$VENV_DIR/bin/python" -c "
import json, sys
try:
    c = json.load(open('$cfg'))
    enabled = [k for k,v in c.get('channels',{}).items() if isinstance(v,dict) and v.get('enabled')]
    print(', '.join(enabled) if enabled else 'none')
except: pass
" 2>/dev/null)"
    [[ -n "$port" ]]     && printf 'port         : %s\n' "$port"
    [[ -n "$channels" ]] && printf 'channels     : %s\n' "$channels"
}

_status_process_uptime() {
    local pid="$1"
    local etime; etime="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')"
    [[ -n "$etime" ]] && printf 'uptime       : %s\n' "$etime"
}

cmd_status() {
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    printf 'workspace    : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    printf 'config file  : %s\n' "$cfg"
    printf 'log file     : %s\n' "$LOG_FILE"
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        _status_config_info
    fi
    printf '\n'
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user status moeka --no-pager || true
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user status nanobot --no-pager || true
    elif [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            ok "running (PID $pid, background)"
            _status_process_uptime "$pid"
        else
            warn "stale PID file — moeka is not running"
        fi
    else
        warn "no gateway process running"
    fi
    if [[ -f "$LOG_FILE" ]]; then
        printf '\n%s--- last 5 log lines ---%s\n' "$_C_DIM" "$_C_RESET"
        tail -n 5 "$LOG_FILE"
    fi
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

    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        local jargs=("--user" "-u" "moeka")
        (( follow )) && jargs+=("-f") || jargs+=("-n" "$lines")
        journalctl "${jargs[@]}" "${extra_args[@]}"
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        local jargs=("--user" "-u" "nanobot")
        (( follow )) && jargs+=("-f") || jargs+=("-n" "$lines")
        journalctl "${jargs[@]}" "${extra_args[@]}"
    elif [[ -f "$LOG_FILE" ]]; then
        printf '%s[%s]%s\n' "$_C_DIM" "$LOG_FILE" "$_C_RESET"
        if (( follow )); then
            tail -f "$LOG_FILE"
        else
            tail -n "$lines" "$LOG_FILE"
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
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"

    printf '%s=== Runtime ===%s\n' "$_C_BLUE" "$_C_RESET"
    command -v python3 >/dev/null 2>&1 \
        && printf 'python3       : %s\n' "$(python3 --version)" \
        || printf 'python3       : %snot installed%s\n' "$_C_RED" "$_C_RESET"
    command -v uv >/dev/null 2>&1 \
        && printf 'uv            : %s\n' "$(uv --version)" \
        || printf 'uv            : %snot installed%s (recommended: https://docs.astral.sh/uv/)\n' "$_C_YELLOW" "$_C_RESET"
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then
        local venv_py; venv_py="$("$VENV_DIR/bin/python" --version 2>&1)"
        local moeka_ver; moeka_ver="$("$VENV_DIR/bin/python" -c "import importlib.metadata; print(importlib.metadata.version('moeka'))" 2>/dev/null || echo "unknown")"
        printf 'venv python   : %s%s%s\n' "$_C_GREEN" "$venv_py" "$_C_RESET"
        printf 'moeka version : %s%s%s\n' "$_C_GREEN" "$moeka_ver" "$_C_RESET"
        printf 'nanobot bin   : %s\n' "$VENV_DIR/bin/nanobot"
    else
        printf 'venv nanobot  : %snot built%s (run ./moeka.sh install)\n' "$_C_RED" "$_C_RESET"
    fi

    printf '\n%s=== Workspace ===%s\n' "$_C_BLUE" "$_C_RESET"
    printf 'workspace     : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    if [[ -d "$MOEKA_WORKSPACE_EXPANDED" ]]; then
        local disk_usage; disk_usage="$(du -sh "$MOEKA_WORKSPACE_EXPANDED" 2>/dev/null | cut -f1)"
        printf 'disk usage    : %s\n' "${disk_usage:-unknown}"
    fi
    [[ -f "$cfg" ]] \
        && printf 'config.json   : %spresent%s\n' "$_C_GREEN" "$_C_RESET" \
        || printf 'config.json   : %smissing%s\n' "$_C_YELLOW" "$_C_RESET"
    [[ -f "${SCRIPT_DIR}/keys.env" ]] \
        && printf 'keys.env      : %spresent (repo)%s\n' "$_C_GREEN" "$_C_RESET" \
        || printf 'keys.env      : %smissing%s (see keys.env.example)\n' "$_C_YELLOW" "$_C_RESET"

    if [[ -f "$cfg" && -x "$VENV_DIR/bin/python" ]]; then
        printf '\n%s=== Config ===%s\n' "$_C_BLUE" "$_C_RESET"
        "$VENV_DIR/bin/python" - <<'PYEOF' "$cfg"
import json, os, sys
cfg_path = sys.argv[1]
try:
    c = json.load(open(cfg_path))
    port = c.get('api', {}).get('port', 8900)
    print(f'api port      : {port}')
    model = c.get('agents', {}).get('defaults', {}).get('model', 'unknown')
    provider = c.get('agents', {}).get('defaults', {}).get('provider', 'unknown')
    print(f'model         : {model}')
    print(f'provider      : {provider}')
    channels = c.get('channels', {})
    enabled  = [k for k,v in channels.items() if isinstance(v,dict) and v.get('enabled')]
    disabled = [k for k,v in channels.items() if isinstance(v,dict) and not v.get('enabled')]
    if enabled:
        print(f'channels on   : {", ".join(enabled)}')
    if disabled:
        print(f'channels off  : {", ".join(disabled)}')
    sudo = c.get('agents', {}).get('defaults', {}).get('allowSudo', False)
    print(f'allow_sudo    : {"enabled" if sudo else "disabled"}')
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
    if systemctl --user is-enabled --quiet moeka 2>/dev/null; then
        local svc_state; svc_state="$(systemctl --user is-active moeka 2>/dev/null; true)"
        svc_state="${svc_state:-unknown}"
        printf 'systemd       : %senabled%s (%s)\n' "$_C_GREEN" "$_C_RESET" "$svc_state"
    else
        printf 'systemd       : not enabled (run ./moeka.sh enable)\n'
    fi
    local linger_state; linger_state="$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')"
    if [[ "$linger_state" == "yes" ]]; then
        printf 'linger        : %senabled%s (autostart on boot OK)\n' "$_C_GREEN" "$_C_RESET"
    else
        printf 'linger        : %sdisabled%s (run: sudo loginctl enable-linger %s)\n' "$_C_YELLOW" "$_C_RESET" "$USER"
    fi
    local _found_pid=""
    if [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            _found_pid="$pid"
        else
            printf 'process       : %sstale PID file%s\n' "$_C_YELLOW" "$_C_RESET"
        fi
    fi
    if [[ -z "$_found_pid" ]]; then
        # Fall back to scanning for the nanobot gateway process (systemd-managed).
        _found_pid="$(pgrep -f 'nanobot gateway' 2>/dev/null | head -1)"
    fi
    if [[ -n "$_found_pid" ]]; then
        local etime; etime="$(ps -p "$_found_pid" -o etime= 2>/dev/null | tr -d ' ')"
        printf 'process       : %srunning%s (PID %s, up %s)\n' "$_C_GREEN" "$_C_RESET" "$_found_pid" "${etime:-?}"
    else
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
            "$VENV_DIR/bin/python" - "$stage/config.json" <<'PY'
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
            warn "add them to keys.env before starting, or run ./moeka.sh telegram-pair / edit keys.env"
        fi
    fi

    cat <<EOF

Next steps:
  1. Edit keys.env to set provider keys and bot tokens.
  2. ./moeka.sh telegram-pair    # if using Telegram (captures token + user id)
  3. ./moeka.sh start            # or ./moeka.sh enable for boot autostart
EOF
}

cmd_new() {
    local name="${1:-}"
    [[ -n "$name" ]] || { err "usage: moeka.sh new NAME [--workspace PATH]"; exit 2; }
    shift || true
    # The top-level parser already consumed --workspace into MOEKA_WORKSPACE_EXPANDED.
    # If the caller passed it, honor that path; otherwise default to ~/.moeka-NAME.
    local ws
    if [[ -n "$WORKSPACE_OVERRIDE" ]]; then
        ws="$MOEKA_WORKSPACE_EXPANDED"
    else
        ws="$HOME/.moeka-$name"
    fi

    if [[ -e "$ws" && -n "$(ls -A "$ws" 2>/dev/null)" ]]; then
        err "target not empty: $ws"
        exit 1
    fi
    local tpl="${SCRIPT_DIR}/templates/workspace"
    [[ -d "$tpl" ]] || { err "templates not found: $tpl"; exit 1; }

    info "scaffolding new workspace: $ws"
    mkdir -p "$ws"
    tar -C "$tpl" -cf - . | tar -C "$ws" -xf -

    # Substitute {{NAME}} / {{USER_NAME}} placeholders in identity files.
    local f
    for f in SOUL.md USER.md; do
        if [[ -f "$ws/$f" ]]; then
            sed -i "s/{{NAME}}/${name}/g; s/{{USER_NAME}}/${name}/g" "$ws/$f"
        fi
    done

    ok "workspace ready: $ws"
    cat <<EOF

To use this instance:
  export MOEKA_WORKSPACE=$ws
  ./moeka.sh telegram-pair    # wire up Telegram (optional)
  ./moeka.sh start

Edit $ws/SOUL.md to define this agent's personality.
Edit $ws/USER.md to describe the user.
EOF
}

cmd_telegram_pair() {
    _ensure_venv
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    local keys="${SCRIPT_DIR}/keys.env"
    [[ -f "$cfg" ]] || { err "config.json not found: $cfg (run ./moeka.sh new or onboard first)"; exit 1; }
    info "pairing Telegram bot"
    info "  config : $cfg"
    info "  keys   : $keys"
    "$VENV_DIR/bin/python" "${SCRIPT_DIR}/scripts/telegram_pair.py" "$keys" "$cfg" "$@"
    local rc=$?
    if (( rc == 0 )); then
        ok "telegram paired — restart moeka to apply: ./moeka.sh restart"
    fi
    return $rc
}

cmd_help() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

case "$CMD" in
    start)          cmd_start "$@" ;;
    run)            cmd_run ;;
    stop)           cmd_stop ;;
    restart)        cmd_restart "$@" ;;
    status)         cmd_status ;;
    logs)           cmd_logs "$@" ;;
    shell)          cmd_shell ;;
    exec)           cmd_exec "$@" ;;
    install)        cmd_install ;;
    version)        cmd_version ;;
    doctor)         cmd_doctor ;;
    enable)         cmd_enable ;;
    disable)        cmd_disable ;;
    export)         cmd_export "$@" ;;
    import)         cmd_import "$@" ;;
    new)            cmd_new "$@" ;;
    telegram-pair)  cmd_telegram_pair "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
        err "unknown command: $CMD"
        cmd_help
        exit 2 ;;
esac
