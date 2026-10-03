---
name: python-type-check
description: "Type-check Python code with pyright and explain errors."
io:
  inputs: {all: [], any: [text/code:python, path]}
  outputs: [text]
  effects: [read-workspace, exec]
---

# python-type-check

Type-check Python code with pyright and explain errors.
