---
name: python-test-runner
description: "Run pytest on a Python project and summarise failures."
io:
  inputs: [path]
  outputs: [text/log]
  effects: [read-workspace, exec]
---

# python-test-runner

Run pytest on a Python project and summarise failures.
