#!/usr/bin/env bash
# Moeka — nanobot for server management.
#
# Usage:
#   ./moeka.sh start            # run the nanobot gateway
#   ./moeka.sh stop             # stop the running instance
#   ./moeka.sh restart          # stop + start
#   ./moeka.sh status           # show service status
#   ./moeka.sh logs [-f]        # tail logs
#   ./moeka.sh shell            # drop into the moeka venv
#   ./moeka.sh exec -- CMD ...  # run a command inside the venv
#   ./moeka.sh install          # install Python deps into .venv
#   ./moeka.sh doctor           # sanity check the environment
#   ./moeka.sh enable           # install + enable systemd user service
#   ./moeka.sh disable          # stop + disable systemd user service
#   ./moeka.sh setup-sudo [-y]  # install passwordless sudo rule (DANGEROUS)
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

# ---------- commands --------------------------------------------------------
cmd_install() {
    _ensure_venv
}

cmd_start() {
    _ensure_venv
    local bin; bin="$(_nanobot_bin)"
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    info "starting gateway: $bin gateway --config $cfg"
    exec "$bin" gateway --config "$cfg" "$@"
}

cmd_stop() {
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user stop moeka
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user stop nanobot
    else
        pkill -f "nanobot gateway" 2>/dev/null || warn "no running gateway found"
    fi
}

cmd_restart() {
    cmd_stop || true
    cmd_start "$@"
}

cmd_status() {
    printf 'workspace    : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    printf 'config file  : %s\n' "${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user status moeka --no-pager || true
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        systemctl --user status nanobot --no-pager || true
    else
        pgrep -af "nanobot gateway" || echo "no gateway process running"
    fi
}

cmd_logs() {
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        journalctl --user -u moeka "$@"
    elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
        journalctl --user -u nanobot "$@"
    else
        warn "no systemd unit active; no log source"
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
    bash "${SCRIPT_DIR}/install-service.sh"
}

cmd_disable() {
    if systemctl --user is-active --quiet moeka 2>/dev/null; then
        systemctl --user stop moeka
    fi
    systemctl --user disable moeka 2>/dev/null || true
    ok "moeka service disabled"
}

cmd_setup_sudo() {
    local sudoers_file="/etc/sudoers.d/moeka-sudo"
    local user; user="$(id -un)"
    local uid; uid="$(id -u)"

    warn "DANGEROUS: about to grant passwordless sudo to user '${user}' (uid ${uid})"
    warn "   This allows any process running as ${user} to run any command as root."
    warn "   Only proceed if moeka's allow_sudo config is intentional."
    warn ""
    warn "   Sudoers file: ${sudoers_file}"
    warn "   Rule: ${user} ALL=(ALL) NOPASSWD: ALL"
    warn ""

    local yes_flag=0
    for arg in "$@"; do [[ "$arg" == "--yes" || "$arg" == "-y" ]] && yes_flag=1; done
    if [[ "$yes_flag" -eq 0 ]]; then
        printf 'Continue? [y/N] '
        read -r reply
        [[ "$reply" =~ ^[Yy]$ ]] || { info "Aborted."; return 0; }
    fi

    local rule="${user} ALL=(ALL) NOPASSWD: ALL"
    if echo "$rule" | sudo tee "$sudoers_file" > /dev/null; then
        sudo chmod 0440 "$sudoers_file"
        ok "Sudoers rule installed: ${sudoers_file}"
        ok "Moeka (running as ${user}) can now use passwordless sudo."
    else
        err "Failed to write ${sudoers_file} — run with sudo or as root."
        exit 1
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

    # Sudo status
    if [[ -f /etc/sudoers.d/moeka-sudo ]]; then
        printf 'sudo rule     : %sinstalled%s (/etc/sudoers.d/moeka-sudo)\n' "$_C_GREEN" "$_C_RESET"
    else
        printf 'sudo rule     : not installed (run ./moeka.sh setup-sudo to enable)\n'
    fi
    if [[ -f "${MOEKA_WORKSPACE_EXPANDED}/config.json" ]]; then
        if grep -q '"allowSudo"\s*:\s*true' "${MOEKA_WORKSPACE_EXPANDED}/config.json" 2>/dev/null; then
            printf 'allow_sudo    : %senabled%s in config\n' "$_C_GREEN" "$_C_RESET"
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
    setup-sudo)  cmd_setup_sudo "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
        err "unknown command: $CMD"
        cmd_help
        exit 2 ;;
esac
