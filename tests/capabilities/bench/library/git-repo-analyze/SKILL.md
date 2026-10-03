---
name: git-repo-analyze
description: "Clone a Git repository from a link and analyse its structure and languages."
io:
  inputs: [url/git]
  outputs: [text/markdown]
  effects: [network, exec, write-workspace]
---

# git-repo-analyze

Clone a Git repository from a link and analyse its structure and languages.
