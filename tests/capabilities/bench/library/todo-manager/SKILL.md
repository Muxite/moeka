---
name: todo-manager
description: "Add, complete and list todo items kept in the workspace."
io:
  inputs: [workspace]
  outputs: [text/markdown]
  effects: [read-workspace, write-workspace]
---

# todo-manager

Add, complete and list todo items kept in the workspace.
