---
name: repo-stats
description: "Count lines of code, commits and contributors of a local repository."
io:
  inputs: [path]
  outputs: [text/markdown]
  effects: [read-workspace, exec]
---

# repo-stats

Count lines of code, commits and contributors of a local repository.
