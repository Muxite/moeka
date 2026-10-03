---
name: json-to-csv
description: "Flatten JSON records into a CSV table."
io:
  inputs: [data/json]
  outputs: [data/csv]
  effects: [read-workspace, write-workspace]
---

# json-to-csv

Flatten JSON records into a CSV table.
