#!/bin/sh
# Entrypoint of the moeka image (spec 005 FR-057). Checks, in order:
#   1. MOEKA_TOKEN_ISSUE_SECRET is set (exit 2 otherwise, before anything starts):
#      the gateway and WebSocket listen on 0.0.0.0 inside the container, which the
#      WebSocket channel only allows with a token issue secret.
#   2. /data is writable by this user (UID 1000), else a non-zero exit naming both.
#   3. First start (no /data/ws/config.json): seed the config from the template with
#      container settings. An existing config is never overwritten.
# Then `exec nanobot "$@"` (default: gateway on /data/ws/config.json).
set -eu

if [ -z "${MOEKA_TOKEN_ISSUE_SECRET:-}" ]; then
    echo "moeka-entrypoint: MOEKA_TOKEN_ISSUE_SECRET is not set; refusing to start" >&2
    echo "  the gateway listens on 0.0.0.0 in the container, so WebSocket clients must" >&2
    echo "  authenticate: set MOEKA_TOKEN_ISSUE_SECRET to a long random value" >&2
    exit 2
fi

: "${MOEKA_OLLAMA_API_BASE:=http://host.docker.internal:11434/v1}"
export MOEKA_OLLAMA_API_BASE
export MOEKA_WORKSPACE="${MOEKA_WORKSPACE:-/data/ws}"

probe="/data/.moeka-write-probe.$$"
if ! ( : > "$probe" ) 2>/dev/null; then
    echo "moeka-entrypoint: /data is not writable by UID $(id -u) (the image runs as UID 1000);" >&2
    echo "  chown the volume to 1000:1000 or mount a named volume at /data" >&2
    exit 1
fi
rm -f "$probe"

ws=/data/ws
template="${MOEKA_TEMPLATE_DIR:-/app/templates/workspace}"
if [ ! -f "$ws/config.json" ]; then
    mkdir -p "$ws"
    python - "$template" "$ws" <<'PY'
import json
import shutil
import sys
from pathlib import Path

template, ws = Path(sys.argv[1]), Path(sys.argv[2])
for item in template.iterdir():
    target = ws / item.name
    if item.name == "config.json" or target.exists():
        continue
    if item.is_dir():
        shutil.copytree(item, target)
    else:
        shutil.copy2(item, target)
config = json.loads((template / "config.json").read_text(encoding="utf-8"))
config.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = "/data/ws"
gateway = config.setdefault("gateway", {})
gateway["host"] = "0.0.0.0"
gateway["port"] = 18790
config.setdefault("channels", {})["websocket"] = {
    "host": "0.0.0.0",
    "port": 8765,
    "tokenIssueSecret": "${MOEKA_TOKEN_ISSUE_SECRET}",
}
config.setdefault("providers", {}).setdefault("ollama", {})["apiBase"] = "${MOEKA_OLLAMA_API_BASE}"
path = ws / "config.json"
tmp = ws / ".config.json.seed"
tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"moeka-entrypoint: seeded {path}", file=sys.stderr)
PY
fi

exec nanobot "$@"
