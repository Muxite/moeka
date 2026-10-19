---
name: media-processing
description: Inspect and process media using available local or configured media tools.
io:
  inputs:
    any: [file:image, file:audio, file:video]
  outputs: [text/markdown, file:image, file:audio]
  effects: [read-workspace, write-workspace, exec, network]
---

# Media processing

Check which image, audio, or video backend is available before promising an operation.
Use the configured media tools first; check executable and model availability for local
processing, and explain when a remote service would receive the file. Work on a copy,
preserve the source, use explicit output paths, and keep logs or extracted text bounded.
Report unsupported formats and backend errors directly.
