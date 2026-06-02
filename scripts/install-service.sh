#!/bin/sh
# Install and enable the Moeka systemd user service.
#
# Safe to run repeatedly. Also cleans up the legacy `nanobot.service` unit
# if one is already installed, so the two don't race.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"

mkdir -p "$SERVICE_DIR"

# Clean up the legacy unit if present — it competes for the same workload.
if systemctl --user list-unit-files nanobot.service >/dev/null 2>&1; then
    if systemctl --user is-active --quiet nanobot 2>/dev/null; then
        echo "stopping legacy nanobot.service"
        systemctl --user stop nanobot || true
    fi
    systemctl --user disable nanobot 2>/dev/null || true
    rm -f "$SERVICE_DIR/nanobot.service"
fi

cp "$SCRIPT_DIR/moeka.service" "$SERVICE_DIR/moeka.service"

systemctl --user daemon-reload
systemctl --user enable moeka
# Use restart so repeated `enable` calls always land on the latest binary/config;
# systemctl restart starts the service if it isn't running yet.
systemctl --user restart moeka

# Enable user lingering — without this, the user manager exits at logout and
# moeka.service will NOT start on boot when no one is logged in (headless boxes).
linger_state="$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')"
if [ "$linger_state" != "yes" ]; then
    echo "enabling user lingering (required for boot autostart on headless systems)"
    if command -v sudo >/dev/null 2>&1; then
        if sudo -n true 2>/dev/null || sudo -v; then
            sudo loginctl enable-linger "$USER" || true
        else
            echo "  sudo unavailable — run manually: sudo loginctl enable-linger $USER" >&2
        fi
    else
        echo "  sudo not found — run manually as root: loginctl enable-linger $USER" >&2
    fi
    linger_state="$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')"
    if [ "$linger_state" != "yes" ]; then
        echo "  WARNING: Linger is still '$linger_state'. moeka will not autostart on boot until this is fixed." >&2
    fi
fi

echo "moeka service installed and started."
echo "  Status: systemctl --user status moeka"
echo "  Logs:   journalctl --user -u moeka -f"
echo "  Stop:   systemctl --user stop moeka"
