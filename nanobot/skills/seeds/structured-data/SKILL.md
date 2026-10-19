---
name: structured-data
description: Inspect, query, validate, and carefully patch JSON, YAML, and CSV data.
io:
  inputs:
    any: [text/query, data/json, data/csv, data/yaml]
  outputs: [data/json, data/csv, text]
  effects: [read-workspace, write-workspace]
---

# Structured data

Use `jq` only after checking that it is installed; otherwise use Python's standard
library (`json`, `csv`, and `tomllib`) or an already installed YAML parser. Validate
input before editing and report the exact field or row when parsing fails. Make patches
to a copy or a narrowly scoped path, preserve unrelated fields and formatting where
possible, then parse the result again. Limit previews to 100 rows or 12,000 characters.
