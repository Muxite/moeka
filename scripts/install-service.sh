#!/usr/bin/env bash
# Install and enable a moeka systemd user service.
#
#   scripts/install-service.sh                  # the default instance: moeka.service
#   scripts/install-service.sh NAME [--no-enable] [--dry-run]
#                                               # instance ~/.moeka-NAME: moeka@NAME.service
#
# Without NAME: safe to run repeatedly; also cleans up the legacy `nanobot.service`
# unit if one is installed, so the two don't race (behaviour unchanged).
#
# With NAME: renders scripts/moeka@.service (the template unit) into
# ~/.config/systemd/user/moeka@.service with this checkout's path, reloads the user
# manager and enables + starts moeka@NAME.service (--no-enable: neither). It never
# touches moeka.service or nanobot.service, never runs sudo, and only prints the
# `loginctl enable-linger` command when linger is off. --dry-run prints the unit and
# the commands and changes nothing.
#
# Exit codes: 0 ok, 1 missing instance, 2 bad name or flag.
# Env: MOEKA_SYSTEMCTL / MOEKA_LOGINCTL replace systemctl / loginctl (tests).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"

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

NAME=""
NO_ENABLE=0
DRY_RUN=0
while (( $# > 0 )); do
    case "$1" in
        --no-enable) NO_ENABLE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "install-service.sh: unknown flag: $1" >&2; exit 2 ;;
        *)
            if [[ -n "$NAME" ]]; then
                echo "install-service.sh: only one instance name may be given" >&2
                exit 2
            fi
            NAME="$1"; shift ;;
    esac
done

USER_NAME="${USER:-$(id -un)}"

# ---------- default instance: moeka.service (unchanged behaviour) -------------
install_default() {
    if (( DRY_RUN )); then
        echo "would install $SERVICE_DIR/moeka.service from $SCRIPT_DIR/moeka.service"
        echo "would run: systemctl --user daemon-reload"
        echo "would run: systemctl --user enable moeka"
        echo "would run: systemctl --user restart moeka"
        return 0
    fi
    mkdir -p "$SERVICE_DIR"

    # Clean up the legacy unit if present — it competes for the same workload.
    if _systemctl --user list-unit-files nanobot.service >/dev/null 2>&1; then
        if _systemctl --user is-active --quiet nanobot 2>/dev/null; then
            echo "stopping legacy nanobot.service"
            _systemctl --user stop nanobot || true
        fi
        _systemctl --user disable nanobot 2>/dev/null || true
        rm -f "$SERVICE_DIR/nanobot.service"
    fi

    cp "$SCRIPT_DIR/moeka.service" "$SERVICE_DIR/moeka.service"

    _systemctl --user daemon-reload
    _systemctl --user enable moeka
    # Use restart so repeated `enable` calls always land on the latest binary/config;
    # systemctl restart starts the service if it isn't running yet.
    _systemctl --user restart moeka

    # Enable user lingering — without this, the user manager exits at logout and
    # moeka.service will NOT start on boot when no one is logged in (headless boxes).
    local linger_state
    linger_state="$(_loginctl show-user "$USER_NAME" 2>/dev/null | sed -n 's/^Linger=//p' || true)"
    if [ "$linger_state" != "yes" ]; then
        echo "enabling user lingering (required for boot autostart on headless systems)"
        if command -v sudo >/dev/null 2>&1; then
            if sudo -n true 2>/dev/null || sudo -v; then
                sudo loginctl enable-linger "$USER_NAME" || true
            else
                echo "  sudo unavailable — run manually: sudo loginctl enable-linger $USER_NAME" >&2
            fi
        else
            echo "  sudo not found — run manually as root: loginctl enable-linger $USER_NAME" >&2
        fi
        linger_state="$(_loginctl show-user "$USER_NAME" 2>/dev/null | sed -n 's/^Linger=//p' || true)"
        if [ "$linger_state" != "yes" ]; then
            echo "  WARNING: Linger is still '$linger_state'. moeka will not autostart on boot until this is fixed." >&2
        fi
    fi

    echo "moeka service installed and started."
    echo "  Status: systemctl --user status moeka"
    echo "  Logs:   journalctl --user -u moeka -f"
    echo "  Stop:   systemctl --user stop moeka"
}

# ---------- named instance: moeka@NAME.service ---------------------------------
install_named() {
    local name="$1"
    if [[ ! "$name" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]]; then
        echo "install-service.sh: invalid instance name '$name' (must match ^[a-z0-9][a-z0-9_-]{0,31}\$)" >&2
        exit 2
    fi
    local root="$HOME/.moeka-$name"
    if [[ ! -f "$root/config.json" ]]; then
        echo "install-service.sh: instance '$name' not found ($root/config.json missing)" >&2
        echo "  create it first: $REPO_DIR/bin/moeka.sh new $name" >&2
        exit 1
    fi
    local template="$SCRIPT_DIR/moeka@.service"
    [[ -f "$template" ]] || { echo "install-service.sh: template missing: $template" >&2; exit 1; }
    local content
    content="$(cat "$template")"
    content="${content//@MOEKA_REPO@/$REPO_DIR}"
    local unit="moeka@${name}.service"
    local dest="$SERVICE_DIR/moeka@.service"
    local -a commands=("--user daemon-reload")
    if (( ! NO_ENABLE )); then
        commands+=("--user enable --now $unit")
    fi

    if (( DRY_RUN )); then
        echo "# $dest"
        printf '%s\n' "$content"
        echo
        for c in "${commands[@]}"; do
            echo "would run: systemctl $c"
        done
        return 0
    fi

    mkdir -p "$SERVICE_DIR"
    local tmp="$SERVICE_DIR/.moeka@.service.$$.tmp"
    printf '%s\n' "$content" > "$tmp"
    mv -f "$tmp" "$dest"
    echo "installed $dest"

    _systemctl --user daemon-reload
    if (( ! NO_ENABLE )); then
        _systemctl --user enable --now "$unit"
        echo "$unit enabled and started."
    else
        echo "$unit not enabled (--no-enable); start it with: systemctl --user enable --now $unit"
    fi

    local linger_state
    linger_state="$(_loginctl show-user "$USER_NAME" 2>/dev/null | sed -n 's/^Linger=//p' || true)"
    if [[ "$linger_state" != "yes" ]]; then
        echo "note: user lingering is off, so $unit will not start at boot without a login."
        echo "  enable it (as an admin): sudo loginctl enable-linger $USER_NAME"
    fi
    echo "  Status: systemctl --user status $unit"
    echo "  Logs:   journalctl --user -u $unit -f"
}

if [[ -z "$NAME" ]]; then
    install_default
else
    install_named "$NAME"
fi
