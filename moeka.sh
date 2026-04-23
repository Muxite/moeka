#!/usr/bin/env bash
# Moeka universal entrypoint.
#
# One command, two modes:
#   * direct mode — python venv on the host (default when Docker is absent)
#   * docker mode — docker compose (when `.dockerized` exists, `--docker`
#     is passed, or $MOEKA_MODE=docker)
#
# Usage:
#   ./moeka.sh start            # run the gateway in the chosen mode
#   ./moeka.sh stop             # stop the running instance
#   ./moeka.sh restart          # stop + start
#   ./moeka.sh status           # show service status
#   ./moeka.sh logs [-f]        # tail logs
#   ./moeka.sh shell            # drop into a shell inside the moeka env
#   ./moeka.sh exec -- CMD ...  # run a command inside the moeka env
#   ./moeka.sh install          # install Python deps / build image
#   ./moeka.sh doctor           # sanity check the environment
#   ./moeka.sh setup-sudo [-y]  # DANGEROUS: install passwordless sudo rule for moeka on the host
#
# Flags (anywhere on the command line):
#   --docker            force docker mode
#   --direct            force direct mode
#   --contained         start in contained mode (docker, no host access)
#   --config PATH       override the config file path
#   --workspace PATH    override MOEKA_WORKSPACE (instance dir, default ~/.nanobot)
#   --state PATH        deprecated alias of --workspace

set -euo pipefail

# ---------- paths & constants -----------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/.venv"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
DOCKERIZED_FLAG="${SCRIPT_DIR}/.dockerized"

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
FORCE_MODE=""                 # empty | direct | docker
CONTAINED_MODE=0             # 1 when --contained is passed
CONFIG_OVERRIDE=""
WORKSPACE_OVERRIDE=""
POSITIONAL=()

while (( $# > 0 )); do
    case "$1" in
        --docker) FORCE_MODE="docker"; shift ;;
        --direct) FORCE_MODE="direct"; shift ;;
        --contained) CONTAINED_MODE=1; FORCE_MODE="docker"; shift ;;
        --config)
            [[ $# -lt 2 ]] && { err "--config requires a path"; exit 2; }
            CONFIG_OVERRIDE="$2"; shift 2 ;;
        --workspace|--state)
            [[ $# -lt 2 ]] && { err "$1 requires a path"; exit 2; }
            [[ "$1" == "--state" ]] && warn "--state is deprecated; use --workspace"
            WORKSPACE_OVERRIDE="$2"; shift 2 ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"
CMD="${1:-help}"
[[ $# -gt 0 ]] && shift || true

# ---------- env loading -----------------------------------------------------
# Source .env and keys.env (in that order, so keys.env wins on conflicts)
# from both the repo root and the workspace. Existing env vars always win.
#
# Usage:
#   _load_env_file <path>
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
elif [[ -n "${MOEKA_STATE:-}" && -z "${MOEKA_WORKSPACE:-}" ]]; then
    # Accept the deprecated env var from older setups.
    warn "MOEKA_STATE is deprecated; please rename to MOEKA_WORKSPACE"
    export MOEKA_WORKSPACE="$MOEKA_STATE"
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

# ---------- mode detection --------------------------------------------------
_detect_mode() {
    if [[ -n "$FORCE_MODE" ]]; then echo "$FORCE_MODE"; return; fi
    if [[ -n "${MOEKA_MODE:-}" && "$MOEKA_MODE" != "auto" ]]; then
        echo "$MOEKA_MODE"; return
    fi
    if [[ -f "$DOCKERIZED_FLAG" ]] && command -v docker >/dev/null 2>&1; then
        echo "docker"; return
    fi
    echo "direct"
}

_compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE" "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose -f "$COMPOSE_FILE" "$@"
    else
        err "docker compose not found"; exit 1
    fi
}

# ---------- direct-mode plumbing --------------------------------------------
_ensure_venv() {
    if [[ -x "$VENV_DIR/bin/nanobot" ]]; then return 0; fi
    info "creating venv at $VENV_DIR"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV_DIR" >/dev/null
        "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
        info "installing nanobot (uv pip install -e .)"
        uv pip install --python "$VENV_DIR/bin/python" -e . >/dev/null
    else
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
        info "installing nanobot (pip install -e .)"
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

_direct_args() {
    local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    printf -- '--config\0%s\0' "$cfg"
}

# ---------- host prep for docker mode ---------------------------------------
_ensure_host_dirs() {
    # The /home:/home bind mount shadows the container's /home/nanobot.
    # Ensure the host has a /home/nanobot owned by uid 1000 so the container
    # user can write dotfiles, caches, etc. in its home directory.
    local nh="/home/nanobot"
    if [[ ! -d "$nh" ]]; then
        info "creating $nh on the host"
        sudo mkdir -p "$nh" 2>/dev/null || mkdir -p "$nh" 2>/dev/null || true
    fi
    if [[ -d "$nh" ]] && [[ "$(stat -c %u "$nh" 2>/dev/null)" != "1000" ]]; then
        info "fixing ownership of $nh to uid 1000"
        sudo chown 1000:1000 "$nh" 2>/dev/null || true
    fi
}

# ---------- commands --------------------------------------------------------
cmd_install() {
    local mode; mode="$(_detect_mode)"
    if [[ "$mode" == "docker" ]]; then
        _ensure_host_dirs
        info "building docker image"
        _compose build
    else
        _ensure_venv
    fi
}

cmd_start() {
    local mode; mode="$(_detect_mode)"
    info "mode = $mode"
    if [[ "$mode" == "docker" ]]; then
        if [[ "$CONTAINED_MODE" -eq 1 ]]; then
            info "starting in contained mode (no host access)"
            _compose up -d moeka-contained
            ok "moeka (contained) started"
        else
            _ensure_host_dirs
            _compose up -d
            ok "moeka (docker) started"
        fi
    else
        _ensure_venv
        local bin; bin="$(_nanobot_bin)"
        local cfg="${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
        info "starting gateway: $bin gateway --config $cfg"
        exec "$bin" gateway --config "$cfg" "$@"
    fi
}

cmd_stop() {
    local mode; mode="$(_detect_mode)"
    if [[ "$mode" == "docker" ]]; then
        _compose down
    else
        # Best-effort: graceful via systemd, then pkill fallback.
        if systemctl --user is-active --quiet moeka 2>/dev/null; then
            systemctl --user stop moeka
        elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
            systemctl --user stop nanobot
        else
            pkill -f "nanobot gateway" 2>/dev/null || warn "no running gateway found"
        fi
    fi
}

cmd_restart() {
    cmd_stop || true
    cmd_start "$@"
}

cmd_status() {
    local mode; mode="$(_detect_mode)"
    printf 'mode         : %s\n' "$mode"
    printf 'workspace    : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    printf 'config file  : %s\n' "${MOEKA_CONFIG:-${MOEKA_WORKSPACE_EXPANDED}/config.json}"
    if [[ "$mode" == "docker" ]]; then
        _compose ps
    else
        if systemctl --user is-active --quiet moeka 2>/dev/null; then
            systemctl --user status moeka --no-pager || true
        elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
            systemctl --user status nanobot --no-pager || true
        else
            pgrep -af "nanobot gateway" || echo "no gateway process running"
        fi
    fi
}

cmd_logs() {
    local mode; mode="$(_detect_mode)"
    if [[ "$mode" == "docker" ]]; then
        _compose logs "$@"
    else
        if systemctl --user is-active --quiet moeka 2>/dev/null; then
            journalctl --user -u moeka "$@"
        elif systemctl --user is-active --quiet nanobot 2>/dev/null; then
            journalctl --user -u nanobot "$@"
        else
            warn "no systemd unit active; no log source"
        fi
    fi
}

cmd_shell() {
    local mode; mode="$(_detect_mode)"
    if [[ "$mode" == "docker" ]]; then
        _compose run --rm nanobot-cli bash
    else
        _ensure_venv
        info "entering moeka venv shell"
        exec "$SHELL" --rcfile <(echo "source $VENV_DIR/bin/activate; PS1='(moeka) \$ '")
    fi
}

cmd_exec() {
    local mode; mode="$(_detect_mode)"
    if [[ "$mode" == "docker" ]]; then
        _compose run --rm nanobot-cli "$@"
    else
        _ensure_venv
        exec "$VENV_DIR/bin/nanobot" "$@"
    fi
}

cmd_setup_sudo() {
    # Install a host-side sudoers rule so that the user moeka runs as
    # (the current $USER, uid $(id -u)) can execute sudo without a password.
    #
    # DANGEROUS: this grants full passwordless sudo to $USER on the host.
    # Only call this if you understand the implications and have enabled
    # tools.exec.allow_sudo in your config.json.
    #
    # When running in docker mode with MOEKA_EXEC_ON_HOST=1, moeka's exec tool
    # wraps commands in `nsenter -t 1 ...` which enters host namespaces but
    # still runs as the calling user (nanobot uid=1000, which maps to $USER on
    # the host).  Passwordless sudo lets moeka escalate on the host when
    # a command requires it.

    local sudoers_file="/etc/sudoers.d/moeka-sudo"
    local user
    user="$(id -un)"
    local uid
    uid="$(id -u)"

    warn "⚠️  DANGEROUS: about to grant passwordless sudo to user '${user}' (uid ${uid})"
    warn "   This allows any process running as ${user} to run any command as root."
    warn "   Only proceed if moeka's allow_sudo config is intentional."
    warn ""
    warn "   Sudoers file: ${sudoers_file}"
    warn "   Rule: ${user} ALL=(ALL) NOPASSWD: ALL"
    warn ""

    # Ask for confirmation unless --yes flag is given.
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
        ok "Moeka (running as ${user}) can now use passwordless sudo on the host."
    else
        err "Failed to write ${sudoers_file} — run with sudo or as root."
        exit 1
    fi
}

cmd_doctor() {
    local mode; mode="$(_detect_mode)"
    printf 'detected mode : %s\n' "$mode"
    command -v docker >/dev/null 2>&1 \
        && printf 'docker        : %s\n' "$(docker --version)" \
        || printf 'docker        : not installed\n'
    command -v python3 >/dev/null 2>&1 \
        && printf 'python3       : %s\n' "$(python3 --version)" \
        || printf 'python3       : not installed\n'
    command -v uv >/dev/null 2>&1 \
        && printf 'uv            : %s\n' "$(uv --version)" \
        || printf 'uv            : not installed\n'
    [[ -x "$VENV_DIR/bin/nanobot" ]] \
        && printf 'venv nanobot  : %s\n' "$VENV_DIR/bin/nanobot" \
        || printf 'venv nanobot  : not built\n'
    printf 'workspace     : %s\n' "$MOEKA_WORKSPACE_EXPANDED"
    [[ -f "${MOEKA_WORKSPACE_EXPANDED}/config.json" ]] \
        && printf 'config.json   : present\n' \
        || printf 'config.json   : %smissing%s\n' "$_C_YELLOW" "$_C_RESET"
    [[ -f "${SCRIPT_DIR}/keys.env" ]] \
        && printf 'keys.env      : present (repo)\n' \
        || printf 'keys.env      : missing (see keys.env.example)\n'

    # Sudo capability check
    if [[ -f /etc/sudoers.d/moeka-sudo ]]; then
        printf 'sudo rule     : %sinstalled%s (/etc/sudoers.d/moeka-sudo)\n' "$_C_GREEN" "$_C_RESET"
    else
        printf 'sudo rule     : not installed (run ./moeka.sh setup-sudo to enable)\n'
    fi
    # Check config.json for allow_sudo
    if [[ -f "${MOEKA_WORKSPACE_EXPANDED}/config.json" ]]; then
        if grep -q '"allowSudo"\s*:\s*true' "${MOEKA_WORKSPACE_EXPANDED}/config.json" 2>/dev/null; then
            printf 'allow_sudo    : %senabled%s in config\n' "$_C_GREEN" "$_C_RESET"
        else
            printf 'allow_sudo    : disabled in config\n'
        fi
    fi
}

cmd_help() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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
    setup-sudo)  cmd_setup_sudo "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
        err "unknown command: $CMD"
        cmd_help
        exit 2 ;;
esac
