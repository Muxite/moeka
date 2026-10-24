# Pair local Moeka instances

Moeka can send bounded work requests between instances over Unix domain sockets. Each
instance runs an authenticated listener at `<workspace>/.nanobot/peer.sock`; the owner
adds the other instance to `peers` in each instance's `config.json` and supplies the
same random secret to both through an environment variable. The recipient receives a
normal inbound turn in its own `peer:<sender-id>` session and uses its own tools and
permissions.

Get each stable instance ID from `<workspace>/.nanobot/workspace-id`. For example, if
the ID of instance B is `bbbb...` and B's workspace is `/home/alex/.moeka-b`, add this
to instance A's config:

```json
{
  "peers": [
    {
      "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "socketPath": "/home/alex/.moeka-b/.nanobot/peer.sock",
      "secret": "${MOEKA_PEER_SHARED_SECRET}"
    }
  ]
}
```

Configure the reciprocal entry on B using A's ID and socket path. Keep the secret out
of chat, workspace notes, and version control. Each outgoing message receives a stable
delivery ID, is stored in the sender's `peers/outbox.sqlite3`, and retries with a
bounded backoff until the receiving instance acknowledges it. Duplicate deliveries
are recognized by ID. Requests are limited to 12,000 characters and peer reply chains
stop after three hops.

Results of work requested from a peer turn return to that turn's saved session key
as a system message, so nested work can continue in the requesting agent. If an
older request has no saved session key, it returns to the paired peer's session.
Results requested from a chat channel are delivered back to that channel.

For a remote peer, set `url` instead of `socketPath`, using the exact
`https://host:port/peer/v1/inbox` endpoint. Configure the receiving instance's
`peerListener` with its HTTPS bind host, port, certificate, and key. TLS certificate
verification is enabled by default, redirects are disabled, and the sender signs each
request with the configured shared secret. The endpoint accepts work only from peer IDs
and secrets listed in the recipient's owner configuration. If the HTTPS hostname points
to a private/VPN address, add only the required address range to `tools.ssrfWhitelist`.

## Wonhee creative routing

The optional `creativeBridge` gateway configuration routes selected Telegram conversations to an
owner-paired creative instance without starting another Telegram poller:

```json
{
  "creativeBridge": {
    "enabled": true,
    "peerId": "<paired instance UUID>",
    "python": "/absolute/path/to/wonhee/.venv/bin/python",
    "cwd": "/absolute/path/to/wonhee",
    "home": "/absolute/path/to/wonhee/data"
  }
}
```

`/wonhee project <name>` selects a project and `/wonhee leave` returns to general chat. The
`wonhee_handoff` tool provides the same selection for natural conversation. Selection is durable per
owner/chat/topic. Creative requests use authenticated structured context and a separate session per
creative project. Text-only peer messages remain compatible.

The bridge imports only files downloaded under this instance's Telegram media directory, plus public
Drive file links. Its local CLI child receives an explicit wonhee data home and a minimal environment.
Outputs must be registered under wonhee's `creative-outbox`, and the recipient must match a destination
observed in authenticated Telegram ingress. The bridge streams uploads and records real Telegram message
ids rather than treating an in-memory enqueue as a delivery acknowledgement.

`/wonhee status` shows transport queue states. A failed or interrupted send may be `uncertain`: Telegram
could have accepted it before the result was recorded. `/wonhee retry` explicitly resends the latest such
text response; it can duplicate an uncertain previous send. File deliveries use wonhee's delivery status
and retry actions. No uncertain send is automatically replayed.

Enabling the bridge also enables durable Telegram update receipts and retains pending Telegram updates.
Queued album members wait for their downloads before a turn is flushed; pending receipts replay after
restart. The gateway `/health` response includes creative receipt and response queue counts.


The signed version1 context includes the project, stable request, original owner/chat/topic route and stored
asset refs. The recipient session is `peer:<sender>:creative:<project>`. The gateway refreshes selection from
wonhee before routing, while replies to delivered artifacts retain those artifacts' projects. These delivery
guards apply to the creative adapter; general peer/channel paths retain their existing contracts. The wonhee
checkout's `docs/production/telegram-creative.md` covers its host-local installer and private outbox service.

For the owner-configured wonhee stdio MCP server, set `forwardCreativeContext: true`. This forwards only the
validated active peer context in MCP request metadata, separately from model arguments. Other MCP servers
keep it disabled, and HTTP transports cannot enable it. Wonhee binds memory source identities and delivery
routes from that context; the model cannot replace them. An unsupported intake source is reported in bounded
signed `input_errors`, while the creator message and successful assets still reach the creative assistant.
