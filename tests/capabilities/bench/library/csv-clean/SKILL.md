---
name: csv-clean
description: "Clean a CSV file: trim whitespace, fix dates, drop duplicate rows."
io:
  inputs: [data/csv]
  outputs: [data/csv]
  effects: [read-workspace, write-workspace]
---

# csv-clean

Clean a CSV file: trim whitespace, fix dates, drop duplicate rows.
