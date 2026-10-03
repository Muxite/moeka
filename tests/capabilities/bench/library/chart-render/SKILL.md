---
name: chart-render
description: "Render a chart or plot from tabular data."
io:
  inputs: {all: [], any: [data/csv, data/json]}
  outputs: [file:image]
  effects: [read-workspace, write-workspace, exec]
---

# chart-render

Render a chart or plot from tabular data.
