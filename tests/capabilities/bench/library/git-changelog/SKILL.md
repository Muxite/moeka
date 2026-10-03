---
name: git-changelog
description: "Write a changelog from the git history of a local repository."
io:
  inputs: [path]
  outputs: [text/markdown]
  effects: [read-workspace, exec]
---

# git-changelog

Write a changelog from the git history of a local repository.
