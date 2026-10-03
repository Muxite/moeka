---
name: audio-trim
description: "Trim or cut an audio file between two timestamps."
io:
  inputs: [file:audio]
  outputs: [file:audio]
  effects: [read-workspace, write-workspace, exec]
---

# audio-trim

Trim or cut an audio file between two timestamps.
