#!/usr/bin/env bash
# Moeka bootstrap — set up moeka on a fresh Ubuntu 24.04 host.
#
# Usage:  ./bootstrap.sh
#
# Idempotent. Safe to re-run.
#
# Steps:
#   1. Verify python3 (>= 3.11) and install `uv` if missing.
#   2. Install moeka into a local venv via ./bin/moeka.sh install.
#   3. Seed keys.env from the example if it doesn't exist.
#   4. Choose workspace bootstrap mode: import / new identity / onboard wizard.
#   5. Optionally pair Telegram.
#   6. Optionally enable systemd autostart.

set -euo pipefail

# This script lives in bin/; cd to the *repo root* (its parent) so
# ./bin/moeka.sh, ./.env and ./keys.env all resolve from the project root.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
info() { printf '%s[bootstrap]%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s[bootstrap]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[bootstrap]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[bootstrap]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

is_interactive() { [[ -t 0 && -t 1 ]]; }
ask() {
    local prompt="$1" default="${2:-}"
    if ! is_interactive; then echo "$default"; return; fi
    local reply
    read -r -p "$prompt" reply
    echo "${reply:-$default}"
}

# ---------- 1. python + uv ---------------------------------------------------
info "checking runtime"
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found. On Ubuntu 24.04: sudo apt update && sudo apt install -y python3 python3-venv"
    exit 1
fi
py_ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
py_major="$(echo "$py_ver" | cut -d. -f1)"
py_minor="$(echo "$py_ver" | cut -d. -f2)"
if (( py_major < 3 || (py_major == 3 && py_minor < 11) )); then
    err "python3 $py_ver is too old (need >= 3.11). Install python3.12 from apt."
    exit 1
fi
ok "python3 $py_ver"

if ! command -v uv >/dev/null 2>&1; then
    info "installing uv (Astral)"
    curl -fsSL https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin or ~/.cargo/bin
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv installed but not on PATH yet. Add ~/.local/bin to PATH and re-run if install fails."
    fi
fi
command -v uv >/dev/null 2>&1 && ok "uv $(uv --version 2>&1 | awk '{print $2}')"

# Common build deps for sentence-transformers / numpy on a clean Ubuntu desktop.
# We don't auto-apt-install — moeka.sh install will tell us if anything is missing.

# ---------- 2. venv + moeka --------------------------------------------------
info "installing moeka into .venv"
./bin/moeka.sh install
ok "moeka installed"

# ---------- 3. keys.env ------------------------------------------------------
if [[ ! -f keys.env ]]; then
    info "creating keys.env from example"
    cp keys.env.example keys.env
    chmod 600 keys.env
    warn "edit keys.env to set provider API keys and bot tokens"
    if is_interactive && command -v "${EDITOR:-}" >/dev/null 2>&1; then
        edit_now="$(ask "open keys.env in \$EDITOR now? [Y/n]: " Y)"
        if [[ "${edit_now,,}" != "n" ]]; then
            "${EDITOR}" keys.env
        fi
    else
        info "(non-interactive — edit $(pwd)/keys.env manually)"
    fi
else
    ok "keys.env already present"
fi

# ---------- 4. workspace setup -----------------------------------------------
if ! is_interactive; then
    info "non-interactive shell — skipping workspace setup prompts"
    info "run one of these next:"
    info "  ./bin/moeka.sh import FILE     # import a workspace archive"
    info "  ./bin/moeka.sh new NAME        # fresh-identity workspace"
    info "  ./bin/moeka.sh exec onboard    # interactive onboard wizard"
    exit 0
fi

echo
info "workspace setup — pick one:"
echo "  [i] import an existing workspace archive (.tar.gz from another moeka)"
echo "  [n] new identity (scaffold a fresh workspace with templates)"
echo "  [o] onboard wizard (interactive nanobot setup)"
echo "  [s] skip (do it later)"
choice="$(ask "choice [i/n/o/s] (default n): " n)"

case "${choice,,}" in
    i)
        archive="$(ask "path to .tar.gz archive: " "")"
        [[ -f "$archive" ]] || { err "file not found: $archive"; exit 1; }
        ws="$(ask "target workspace path [default: ~/.nanobot]: " "$HOME/.nanobot")"
        MOEKA_WORKSPACE="$ws" ./bin/moeka.sh import "$archive"
        ;;
    n)
        name="$(ask "name for this moeka instance (e.g. alice): " "moeka")"
        ws="$HOME/.moeka-$name"
        ./bin/moeka.sh new "$name" --workspace "$ws"
        warn "remember to set MOEKA_WORKSPACE=$ws in your shell or .env"
        # Persist for the bootstrap session by appending to .env (gitignored).
        if [[ ! -f .env ]] || ! grep -q '^MOEKA_WORKSPACE=' .env; then
            printf 'MOEKA_WORKSPACE=%s\n' "$ws" >> .env
            ok "wrote MOEKA_WORKSPACE=$ws to ./.env"
        fi
        export MOEKA_WORKSPACE="$ws"
        ;;
    o)
        ./bin/moeka.sh exec onboard
        ;;
    s|*)
        info "skipping workspace setup"
        ;;
esac

# ---------- 5. telegram ------------------------------------------------------
echo
do_tg="$(ask "pair a Telegram bot now? [y/N]: " N)"
if [[ "${do_tg,,}" == "y" ]]; then
    ./bin/moeka.sh telegram-pair || warn "telegram pairing did not complete"
fi

# ---------- 6. systemd -------------------------------------------------------
echo
do_enable="$(ask "enable systemd autostart (recommended for always-on)? [Y/n]: " Y)"
if [[ "${do_enable,,}" != "n" ]]; then
    ./bin/moeka.sh enable
fi

echo
ok "bootstrap complete"
./bin/moeka.sh doctor || true
