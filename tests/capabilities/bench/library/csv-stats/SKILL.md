---
name: csv-stats
description: "Compute summary statistics (mean, median, counts) of CSV columns."
io:
  inputs: [data/csv]
  outputs: [text/markdown, data/csv]
  effects: [read-workspace]
---

# csv-stats

Compute summary statistics (mean, median, counts) of CSV columns.
