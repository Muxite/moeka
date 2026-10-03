---
name: stacktrace-explain
description: "Explain a Python or Java stack trace and suggest a fix."
io:
  inputs: {all: [], any: [text/log, text/code]}
  outputs: [text/markdown]
  effects: [none]
---

# stacktrace-explain

Explain a Python or Java stack trace and suggest a fix.
