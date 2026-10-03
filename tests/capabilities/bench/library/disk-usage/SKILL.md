---
name: disk-usage
description: "Show which folders under a directory use the most disk space."
io:
  inputs: [path]
  outputs: [text]
  effects: [read-workspace, exec]
---

# disk-usage

Show which folders under a directory use the most disk space.
