---
name: document-processing
description: Extract and summarize supported documents through installed parsers.
io:
  inputs:
    any: [file:pdf, file:office]
  outputs: [text, text/markdown]
  effects: [read-workspace, exec]
---

# Document processing

Check the file type and available parser before processing. Prefer the application's
document extraction backend or an installed parser; do not install packages. For large
documents, extract only relevant pages or sections and cap returned text at 12,000
characters. Preserve page numbers and quote short passages only. If parsing fails,
report the file type and parser error without claiming the document was read.
