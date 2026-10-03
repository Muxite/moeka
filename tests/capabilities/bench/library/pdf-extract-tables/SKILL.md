---
name: pdf-extract-tables
description: "Extract tables from a PDF into CSV files, one per table."
io:
  inputs: [file:pdf]
  outputs: [data/csv]
  effects: [read-workspace, write-workspace]
---

# pdf-extract-tables

Extract tables from a PDF into CSV files, one per table.
