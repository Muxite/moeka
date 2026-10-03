---
name: video-transcribe
description: "Transcribe the speech in a video or online video link."
io:
  inputs: {all: [], any: [file:video, url/video]}
  outputs: [text/markdown]
  effects: [read-workspace, network, exec]
---

# video-transcribe

Transcribe the speech in a video or online video link.
