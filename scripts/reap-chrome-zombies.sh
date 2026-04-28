#!/usr/bin/env bash
# Kill leaked chromedriver and chrome processes belonging to the current user.
#
# The Jennie Scraper leaks chromedriver/chrome workers that eventually exhaust
# system resources and cause HTTP 502 errors.  Run this manually or wire it
# into a cron job / systemd timer to reap stale processes automatically.
#
# Usage:
#   bash scripts/reap-chrome-zombies.sh [--dry-run]
#
# Options:
#   --dry-run   Print what would be killed without actually killing anything.

set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

_kill_pattern() {
    local label="$1" pattern="$2"
    local pids
    pids=$(pgrep -u "$USER" -f "$pattern" 2>/dev/null || true)
    if [[ -z "$pids" ]]; then
        echo "[reap] no ${label} processes found"
        return
    fi
    local count
    count=$(echo "$pids" | wc -w)
    if $DRY_RUN; then
        echo "[reap][dry-run] would kill ${count} ${label} process(es): ${pids}"
    else
        echo "[reap] killing ${count} ${label} process(es): ${pids}"
        echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

_kill_pattern "chromedriver" "chromedriver"
_kill_pattern "chrome/chromium" "(chrome|chromium-browser|chromium) "

if ! $DRY_RUN; then
    echo "[reap] reaping orphaned zombie children (wait -n)..."
    while true; do
        local_pid=$(wait -n 2>/dev/null && echo $? || true)
        [[ -z "$local_pid" ]] && break
    done 2>/dev/null || true
    echo "[reap] done"
fi
