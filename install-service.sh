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
systemctl --user restart moeka

echo "moeka service installed and started."
echo "  Status: systemctl --user status moeka"
echo "  Logs:   journalctl --user -u moeka -f"
echo "  Stop:   systemctl --user stop moeka"
