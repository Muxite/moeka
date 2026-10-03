---
name: sql-from-csv
description: "Load a CSV into SQLite and answer questions with SQL."
io:
  inputs: [data/csv]
  outputs: [data/csv, text]
  effects: [read-workspace, exec]
---

# sql-from-csv

Load a CSV into SQLite and answer questions with SQL.
