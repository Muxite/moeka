---
name: pdf-ocr
description: "Run OCR on scanned PDFs or images to recover searchable text."
io:
  inputs: {all: [], any: [file:pdf, file:image]}
  outputs: [text]
  effects: [read-workspace, exec]
---

# pdf-ocr

Run OCR on scanned PDFs or images to recover searchable text.
