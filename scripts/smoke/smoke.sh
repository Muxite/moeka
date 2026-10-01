#!/usr/bin/env bash
# Gateway smoke test, runs INSIDE the throwaway test container (see scripts/smoke-gateway.sh).
# Isolated: mock LLM on 29977, gateway 28790, websocket 28765, API 28900; no real tokens/keys.
set -u
export HOME=/tmp/smoke/home MOEKA_WORKSPACE=/tmp/smoke/ws PYTHONDONTWRITEBYTECODE=1
mkdir -p $HOME $MOEKA_WORKSPACE
cd /app
CFG=/tmp/smoke/config.json
cat > $CFG <<J
{
  "agents": {"defaults": {"model": "custom/mock-model", "workspace": "/tmp/smoke/ws"}},
  "providers": {"custom": {"apiKey": "mock-key-not-real", "apiBase": "http://127.0.0.1:29977/v1"}},
  "gateway": {"host": "127.0.0.1", "port": 28790, "heartbeat": {"enabled": false}},
  "api": {"host": "127.0.0.1", "port": 28900},
  "channels": {"sendProgress": false, "websocket": {"enabled": true, "port": 28765}},
  "agents_note": "isolated smoke config"
}
J
python3 - <<'P'
import json; p="/tmp/smoke/config.json"; d=json.load(open(p)); d.pop("agents_note"); json.dump(d,open(p,"w"))
P
echo "== 1. import moeka / kernel facade"
uv run --no-sync python - <<'P'
import moeka, nanobot.kernel, sys
print("moeka", moeka.__name__, "Kernel" in dir(moeka))
import nanobot.agent.loop  # noqa
bad=[m for m in sys.modules if m.split(".")[:2] in (["nanobot","channels"],["nanobot","webui"],["nanobot","cron"],["nanobot","gateway"],["nanobot","triggers"],["nanobot","apps"])]
print("host modules loaded by kernel import:", bad)
P
python3 /tmp/smoke/mock_llm.py 29977 & MOCK=$!
sleep 1
echo "== 2. start gateway (foreground, background job)"
uv run --no-sync nanobot gateway --foreground --config $CFG --workspace /tmp/smoke/ws --port 28790 > /tmp/smoke/gateway.log 2>&1 & GW=$!
for i in $(seq 1 40); do
  if python3 /tmp/smoke/get.py http://127.0.0.1:28790/health >/tmp/smoke/health.json 2>/dev/null; then break; fi; sleep 1
done
echo "health:"; cat /tmp/smoke/health.json; echo
echo "== 3. mock chat turn via CLI agent (host loop: cron+message tools)"
timeout 120 uv run --no-sync nanobot agent --config $CFG --workspace /tmp/smoke/ws -m "say hi" --no-markdown 2>&1 | tail -8
echo "== 4. mock chat turn via HTTP API server (nanobot serve)"
uv run --no-sync nanobot serve --config $CFG --workspace /tmp/smoke/ws --port 28900 > /tmp/smoke/serve.log 2>&1 & SV=$!
for i in $(seq 1 30); do python3 /tmp/smoke/get.py http://127.0.0.1:28900/health >/dev/null 2>&1 && break; sleep 1; done
python3 /tmp/smoke/get.py http://127.0.0.1:28900/v1/chat/completions '{"model":"custom/mock-model","messages":[{"role":"user","content":"hello"}]}' | head -c 700; echo
echo "== gateway log tail"; tail -25 /tmp/smoke/gateway.log
echo "== serve log tail"; tail -8 /tmp/smoke/serve.log
echo "== 5. stop"
kill -TERM $SV $GW $MOCK 2>/dev/null; sleep 3; kill -9 $SV $GW $MOCK 2>/dev/null; wait 2>/dev/null
echo done
