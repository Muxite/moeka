# Transfer certified capabilities between instances

Moeka transfers skills and script tools only when their certificates verify in both
the source and destination trust stores. The destination's `capabilities.allowedEffects`
must include every declared effect. Transfer never copies a personality, memory,
conversation, credential, or schedule.
Capabilities declaring a `sudo` effect are refused for every destination.

The RSI evaluator and signing key stay outside Moeka. This CLI checks signed
certificates and current revocations; it does not judge or certify a capability.
Transfers are manual and automatic promotion is off. Choose an idle point between
recipient turns for activation so the recipient does not read a bundle while it is
being replaced.

Preview a skill before staging it:

```sh
moeka capabilities transfer preview skill portable-skill \
  --source-config ~/.nanobot/config.json \
  --destination-config ~/.moeka-wonny/config.json
```

Omit either config option to use the default `~/.nanobot/config.json`. Workspace
overrides are also available with `--source-workspace` and
`--destination-workspace`.

Stage and approve as separate steps:

```sh
moeka capabilities transfer stage skill portable-skill \
  --source-config ~/.nanobot/config.json \
  --destination-config ~/.moeka-wonny/config.json
moeka capabilities transfer activate STAGE_ID \
  --config ~/.moeka-wonny/config.json
```

`stage` prints the ID. The `activate` command records owner approval. A skill applies
before the next turn while the agent is idle; if another request is running, the
approval waits for a later idle boundary. Script tools apply at the next process start,
before Moeka builds its tool registry. Activation repeats destination certificate,
revocation, and effect checks. It records the displaced destination bundle inside that
workspace's `.moeka-transfer/history/` directory. Inspect and undo activations with:

```sh
moeka capabilities transfer history --config ~/.moeka-wonny/config.json
moeka capabilities transfer rollback --config ~/.moeka-wonny/config.json
```

Rollback restores the previous bundle only while its certificate remains trusted and
its effects still fit the destination's permissions. It removes a capability if the
activation introduced it for the first time. Trust stores, permissions, or bundles
that fail those checks are refused.

Runtime profile changes need an owner-reviewed configuration update. Changes to
Moeka's core need a reviewed patch against the current main branch, acceptance checks,
and an explicit update for each destination; update RSI's pinned core separately.
Capability transfer never applies configuration or core patches.

The `moeka` command invokes the same CLI as the compatibility command `nanobot`.
Telegram, Discord, and other chat channel runtimes remain optional integrations and
can be enabled per instance with `moeka channels enable NAME`.
