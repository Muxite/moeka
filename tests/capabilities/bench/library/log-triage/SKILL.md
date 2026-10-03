---
name: log-triage
description: "Triage a log file: group errors, find the first failure and likely cause."
io:
  inputs: [text/log]
  outputs: [text/markdown]
  effects: [read-workspace]
---

# log-triage

Triage a log file: group errors, find the first failure and likely cause.
