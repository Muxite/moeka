#!/bin/sh
# Restart Moeka via systemd — safe to call from inside Moeka itself.
# Accepts both the new `moeka` unit name and the legacy `nanobot` unit.
if systemctl --user list-unit-files moeka.service >/dev/null 2>&1 \
        && systemctl --user is-enabled --quiet moeka 2>/dev/null; then
    exec systemctl --user restart moeka
fi
exec systemctl --user restart nanobot
