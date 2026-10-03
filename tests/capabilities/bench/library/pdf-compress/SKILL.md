---
name: pdf-compress
description: "Shrink a PDF's file size by recompressing images."
io:
  inputs: [file:pdf]
  outputs: [file:pdf]
  effects: [read-workspace, write-workspace, exec]
---

# pdf-compress

Shrink a PDF's file size by recompressing images.
