---
name: systemd-status
description: "Check the status of systemd services on this host and restart failed ones."
io:
  inputs: [host]
  outputs: [text]
  effects: [exec]
---

# systemd-status

Check the status of systemd services on this host and restart failed ones.
