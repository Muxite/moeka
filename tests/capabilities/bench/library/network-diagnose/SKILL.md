---
name: network-diagnose
description: "Diagnose this host's network: wifi, DNS and routing problems."
io:
  inputs: [host]
  outputs: [text/markdown]
  effects: [exec, network]
---

# network-diagnose

Diagnose this host's network: wifi, DNS and routing problems.
