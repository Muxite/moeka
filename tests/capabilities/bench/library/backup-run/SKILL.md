---
name: backup-run
description: "Run the configured backup job of this host and verify it."
io:
  inputs: [host]
  outputs: [text]
  effects: [exec, write-workspace]
---

# backup-run

Run the configured backup job of this host and verify it.
