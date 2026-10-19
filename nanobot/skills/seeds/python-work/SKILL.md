---
name: python-work
description: Run small Python analyses and scripts using the existing execution backend.
io:
  inputs: [text/query]
  outputs: [text, data/json]
  effects: [read-workspace, write-workspace, exec]
---

# Python work

Use the configured Python execution tool or the project's existing virtual environment.
Check `python --version` and required imports before running a script. Prefer a short
script with explicit input and output paths. Bound printed output and catch expected
file, decode, and validation errors; do not hide unexpected tracebacks. Never install
packages as a side effect. Ask before running scripts that modify many files.
