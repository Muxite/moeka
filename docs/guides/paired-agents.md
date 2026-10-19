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

For a remote peer, set `url` instead of `socketPath`, using the exact
`https://host:port/peer/v1/inbox` endpoint. Configure the receiving instance's
`peerListener` with its HTTPS bind host, port, certificate, and key. TLS certificate
verification is enabled by default, redirects are disabled, and the sender signs each
request with the configured shared secret. The endpoint accepts work only from peer IDs
and secrets listed in the recipient's owner configuration. If the HTTPS hostname points
to a private/VPN address, add only the required address range to `tools.ssrfWhitelist`.
