---
name: dependency-audit
description: "Audit a project's dependencies for known vulnerabilities."
io:
  inputs: [path]
  outputs: [text/markdown]
  effects: [read-workspace, network, exec]
---

# dependency-audit

Audit a project's dependencies for known vulnerabilities.
