---
name: audio-normalize
description: "Normalise loudness of audio tracks."
io:
  inputs: [file:audio]
  outputs: [file:audio]
  effects: [read-workspace, write-workspace, exec]
---

# audio-normalize

Normalise loudness of audio tracks.
