---
name: git-blame-explain
description: "Explain who changed a line of a file and why, using git blame."
io:
  inputs: [path]
  outputs: [text/markdown]
  effects: [read-workspace, exec]
---

# git-blame-explain

Explain who changed a line of a file and why, using git blame.
