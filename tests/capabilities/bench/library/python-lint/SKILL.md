---
name: python-lint
description: "Lint Python code with ruff and explain the warnings."
io:
  inputs: {all: [], any: [text/code:python, path]}
  outputs: [text]
  effects: [read-workspace, exec]
---

# python-lint

Lint Python code with ruff and explain the warnings.
