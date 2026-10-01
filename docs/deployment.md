# Deployment

Use this page after `nanobot agent -m "Hello!"` works locally. Deployment keeps long-running surfaces online: WebUI, chat apps, heartbeat, Dream, cron jobs, and channel connections.

## Before You Deploy

Check these once before Docker, systemd, or LaunchAgent:

| Check | Why it matters |
|---|---|
| `nanobot status` shows the expected config and workspace | Confirms the process will read the instance you meant to run |
| `nanobot agent -m "Hello!"` works | Proves install, config, provider, model, and workspace writes before adding a service layer |
| Secrets are in environment variables or protected config files | API keys, bot tokens, OAuth state, and chat credentials should not be world-readable |
| The active config directory (including `sessions/`) and workspace are persistent | Sessions follow `--config`; memory, generated artifacts, and the workspace identity marker follow the workspace |
| Channel access control is intentional | Use `allowFrom`, pairing, WebSocket `token`/`tokenIssueSecret`, or private test channels before exposing the bot |
| Ports are planned | Gateway health defaults to local-only `127.0.0.1:18790`; WebUI/WebSocket defaults to `8765`; `nanobot serve` defaults to `8900` |
| Logs are easy to reach | Use `docker compose logs`, `journalctl`, LaunchAgent log files, or `nanobot gateway --verbose` while diagnosing startup |

Restart the deployed process after editing `config.json`. Long-running processes read config at startup.


## Choose a Runtime

| Runtime | Use it for | State location | Useful first command |
|---|---|---|---|
| `bin/moeka.sh` + systemd user units | Several host instances, each with its own unit | `~/.nanobot` or `~/.moeka-<name>` | `./bin/moeka.sh list` |
| Docker Compose | Repeatable container instances next to host ones | Named volume at `/data` | `docker compose -p a up -d` |
| Docker CLI | Manual container testing or one-off hosts | Named volume at `/data` | `docker run --rm --entrypoint id moeka -u` |
| systemd user service (`nanobot gateway install-service`) | A generic unit for one gateway | Host user's `~/.nanobot` unless you pass explicit paths | `nanobot gateway install-service --dry-run` |
| macOS LaunchAgent | macOS gateway that starts after login | Host user's `~/.nanobot` unless the plist passes explicit paths | `launchctl list | grep ai.nanobot.gateway` |

The upstream Render blueprint (`render.yaml`) predates this fork's image and is not
maintained here; it is not a supported runtime.

Several instances on one machine (ports, units, locks, budgets) are covered in
[`multiple-instances.md`](multiple-instances.md).

## Docker

The repository's `Dockerfile` builds a minimal gateway image and `compose.yaml` runs
it. One container is one instance:

- The image runs as UID/GID 1000 (`USER 1000:1000`) and contains no secret.
- `/data` is a volume. The instance root is `/data/ws` (`MOEKA_WORKSPACE=/data/ws`) and
  its sessions live in `/data/ws-sessions/`.
- The entrypoint `scripts/container-entrypoint.sh` (installed as
  `/usr/local/bin/moeka-entrypoint`) exits 2 before starting anything when
  `MOEKA_TOKEN_ISSUE_SECRET` is unset or empty, and exits non-zero naming `/data` and
  UID 1000 when the volume is not writable.
- On first start (no `/data/ws/config.json`) it seeds the config from
  `templates/workspace/config.json` with `agents.defaults.workspace = "/data/ws"`,
  `gateway.host = "0.0.0.0"`, `gateway.port = 18790`, a WebSocket channel on
  `0.0.0.0:8765` with `tokenIssueSecret: "${MOEKA_TOKEN_ISSUE_SECRET}"`, and
  `providers.ollama.apiBase = "${MOEKA_OLLAMA_API_BASE}"`. An existing config is never
  overwritten.
- The default command is `gateway --config /data/ws/config.json --workspace /data/ws`.

### Build and run

```bash
docker build -t moeka .                         # EXTRAS build arg, e.g. --build-arg EXTRAS=vec
docker run --rm --entrypoint id moeka -u        # prints 1000

docker run -d --name moeka-a \
  -e MOEKA_TOKEN_ISSUE_SECRET="$(openssl rand -hex 32)" \
  -p 127.0.0.1:18790:18790 -p 127.0.0.1:8765:8765 \
  -v moeka-a-data:/data \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --add-host host.docker.internal:host-gateway \
  moeka
curl http://127.0.0.1:18790/health
```

### Compose: several instances

`compose.yaml` has one service, `moeka`. It runs as `1000:1000`, mounts the named
volume `moeka-data` at `/data`, publishes the gateway and WebSocket ports on
`127.0.0.1` only (host ports `MOEKA_GATEWAY_PORT`, default 18790, and `MOEKA_WS_PORT`,
default 8765), requires `MOEKA_TOKEN_ISSUE_SECRET`, passes `MOEKA_OLLAMA_API_BASE`,
maps `host.docker.internal` to the host gateway, drops all capabilities, sets
`no-new-privileges`, and health-checks the gateway's `/health`. Provider keys and bot
tokens go in an optional `./moeka.env` next to the file (never into the image).

Every project name gets its own volume (`<project>_moeka-data`), so several instances
run side by side with different host ports:

```bash
export MOEKA_TOKEN_ISSUE_SECRET="$(openssl rand -hex 32)"
MOEKA_GATEWAY_PORT=18791 MOEKA_WS_PORT=8766 docker compose -p a up -d
MOEKA_GATEWAY_PORT=18792 MOEKA_WS_PORT=8767 docker compose -p b up -d
docker compose -p a logs -f moeka
docker compose -p a down        # keeps the volume; `down -v` deletes it
```

### Binding to 0.0.0.0 needs tokenIssueSecret

Docker port forwarding cannot reach a container's loopback interface, so inside the
container the gateway and the WebSocket channel bind `0.0.0.0`. The WebSocket channel
refuses to start on `0.0.0.0` unless `token`, `tokenIssueSecret` or a fully configured
`trustedProxyAuth` is set; the seeded config uses `tokenIssueSecret` from
`MOEKA_TOKEN_ISSUE_SECRET`, which is why the entrypoint requires it. Publish the ports on
`127.0.0.1` (as `compose.yaml` does): the gateway health route is minimal and
unauthenticated. See [`webui.md#lan-access`](./webui.md#lan-access).

### Ollama on the host

`MOEKA_OLLAMA_API_BASE` defaults to `http://host.docker.internal:11434/v1`, and
`host.docker.internal` maps to the host gateway, so a host Ollama is reachable from the
container. Override it for another endpoint:

```bash
MOEKA_OLLAMA_API_BASE=http://10.0.0.5:11434/v1 docker compose -p a up -d
```

Ollama must listen on an address the container can reach (for example
`OLLAMA_HOST=0.0.0.0`, firewalled to the Docker bridge).

### bwrap inside a container

`tools.exec.sandbox: "bwrap"` needs `CAP_SYS_ADMIN` and user namespaces, which the
default container (all capabilities dropped, `no-new-privileges`) does not have: the
exec tool then fails just as it does on a host without namespaces. The container itself
is the isolation layer for shell execution; grant `CAP_SYS_ADMIN` only if you
deliberately want nested bwrap sandboxes.

### Cloudflare Tunnel + Cloudflare Access

For a local `cloudflared` process in front of nanobot, Cloudflare Access can
authenticate the user before forwarding the request and add
`Cf-Access-Jwt-Assertion`. Opt in to trusted-proxy no-token mode only when the
direct TCP peer is the tunnel process and the assertion is non-empty:

```json
{
  "gateway": { "host": "127.0.0.1" },
  "channels": {
    "websocket": {
      "host": "127.0.0.1",
      "port": 8765,
      "publicWsUrl": "wss://nanobot.example.com/",
      "trustedProxyAuth": {
        "trustedPeerCidrs": ["127.0.0.1/32", "::1/128"],
        "assertionHeader": "Cf-Access-Jwt-Assertion"
      }
    }
  }
}
```

This is two-part authorization: a trusted direct loopback peer **and** a
non-empty Cloudflare Access assertion. A trusted CIDR alone is not a bypass.
For this flow `/webui/bootstrap` returns connection metadata without a
bootstrap token or REST API token; the proxy assertion authorizes the WebSocket
handshake and REST requests directly.

Set `publicWsUrl` to the browser-facing `wss://` endpoint when the tunnel sends
the origin host header (such as `127.0.0.1:8765`); otherwise the WebUI could
attempt to open its WebSocket directly against the loopback address.
The assertion header must be generated
by Cloudflare Access after authentication; routing/client metadata headers such
as `Host`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, and `CF-Connecting-IP`
are rejected as `assertionHeader` values. Nanobot trusts the assertion but does
not cryptographically validate the JWT, so configure the tunnel and Access
policy carefully and do not expose the nanobot listener directly to untrusted
clients. Forwarded client headers do not establish proxy trust.


## Linux Service

### moeka instances (recommended)

The default instance runs under `moeka.service` (`./bin/moeka.sh enable`); each named
instance `~/.moeka-<name>` runs under the template unit `moeka@<name>.service`:

```bash
./bin/moeka.sh new work                         # create ~/.moeka-work
scripts/install-service.sh work --dry-run       # preview the rendered unit
scripts/install-service.sh work                 # install moeka@.service, enable --now
systemctl --user status moeka@work.service
journalctl --user -u moeka@work.service -f
./bin/moeka.sh --workspace ~/.moeka-work disable
```

`scripts/install-service.sh <name>` writes `~/.config/systemd/user/moeka@.service`
(from `scripts/moeka@.service`, with this checkout's path), runs
`systemctl --user daemon-reload` and `systemctl --user enable --now moeka@<name>.service`
(`--no-enable` skips both enable and start). It never touches `moeka.service` or
`nanobot.service` and never runs `sudo`; when lingering is off it prints the
`loginctl enable-linger` command for you.

### Generic gateway unit

`nanobot gateway install-service` writes a unit named after `--name` for one gateway.
Preview it first:

```bash
nanobot gateway install-service --manager systemd --name moeka-gateway --dry-run
```

Install, enable, and start it, passing the same config/workspace selector you use to
run the gateway:

```bash
nanobot gateway install-service \
  --manager systemd \
  --name moeka-gateway \
  --config ~/.moeka-work/config.json \
  --workspace ~/.moeka-work
```

Common operations:

```bash
systemctl --user status moeka-gateway        # check status
systemctl --user restart moeka-gateway       # restart after config changes
journalctl --user -u moeka-gateway -f        # follow logs
nanobot gateway uninstall-service --manager systemd --name moeka-gateway
```

The installer writes `~/.config/systemd/user/<name>.service`, runs
`systemctl --user daemon-reload`, enables the unit, and restarts it. It uses the
current Python executable with `python -m nanobot gateway --foreground`, so the
service runs in the same environment you used to install nanobot.

> **Note:** User services only run while you are logged in. To keep the gateway running after logout, enable lingering:
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

Use a LaunchAgent when you want `nanobot gateway` to stay online after you log in, without keeping a terminal open.

Preview the generated plist first:

```bash
nanobot gateway install-service --manager launchd --dry-run
```

Install, load, enable, and start it:

```bash
nanobot gateway install-service --manager launchd
```

For a custom instance:

```bash
nanobot gateway install-service \
  --manager launchd \
  --name nanobot-telegram \
  --config ~/.nanobot-telegram/config.json \
  --workspace ~/.nanobot-telegram/workspace
```

Common operations:

```bash
launchctl list | grep ai.nanobot.gateway
launchctl kickstart -k gui/$(id -u)/ai.nanobot.gateway
nanobot gateway uninstall-service --manager launchd
```

The installer writes `~/Library/LaunchAgents/ai.nanobot.gateway.plist`, uses the
current Python executable with `python -m nanobot gateway --foreground`, and
writes LaunchAgent logs under the instance data dir's `logs/` (the config's directory,
else the workspace, else the state home).

> **Note:** if startup fails with "address already in use", stop the manually started `nanobot gateway` process first.
