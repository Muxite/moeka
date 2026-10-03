---
name: file-checksum
description: "Compute sha256 checksums of files and verify them."
io:
  inputs: [path]
  outputs: [text]
  effects: [read-workspace]
---

# file-checksum

Compute sha256 checksums of files and verify them.
