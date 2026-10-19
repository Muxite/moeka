---
name: test-runner
description: Run focused project tests and checks with concise, bounded results.
io:
  inputs: [text/query]
  outputs: [text/log]
  effects: [read-workspace, exec]
---

# Test runner

Inspect the project instructions and test configuration before running anything. Check
that the requested runner exists (`python -m pytest`, `pytest`, `npm test`, or the local
project command); if it is missing, report that dependency instead of installing it.
Prefer the narrowest test that covers the change, then the project's required check.
Set a reasonable timeout. Return the exit status and the last 80 lines of output; do
not paste whole logs. Do not rewrite snapshots or run formatters unless requested.
