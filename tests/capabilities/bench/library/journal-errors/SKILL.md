---
name: journal-errors
description: "Show recent errors from the systemd journal of this machine."
io:
  inputs: [host]
  outputs: [text/log]
  effects: [exec]
---

# journal-errors

Show recent errors from the systemd journal of this machine.
