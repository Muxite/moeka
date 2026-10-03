---
name: pdf-redact
description: "Redact names, emails and phone numbers from a PDF before sharing."
io:
  inputs: [file:pdf]
  outputs: [file:pdf]
  effects: [read-workspace, write-workspace]
---

# pdf-redact

Redact names, emails and phone numbers from a PDF before sharing.
