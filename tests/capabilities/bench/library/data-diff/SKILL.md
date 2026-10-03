---
name: data-diff
description: "Diff two datasets (csv or json) and report changed rows."
io:
  inputs: {all: [], any: [data/csv, data/json]}
  outputs: [text/markdown]
  effects: [read-workspace]
---

# data-diff

Diff two datasets (csv or json) and report changed rows.
