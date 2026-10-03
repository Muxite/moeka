---
name: xlsx-to-csv
description: "Export the sheets of an Excel workbook to CSV."
io:
  inputs: [file:office]
  outputs: [data/csv]
  effects: [read-workspace, write-workspace]
---

# xlsx-to-csv

Export the sheets of an Excel workbook to CSV.
