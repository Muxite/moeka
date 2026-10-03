---
name: audio-transcribe
description: "Transcribe speech in an audio recording to text."
io:
  inputs: [file:audio]
  outputs: [text/markdown]
  effects: [read-workspace, network]
---

# audio-transcribe

Transcribe speech in an audio recording to text.
