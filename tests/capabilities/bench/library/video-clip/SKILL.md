---
name: video-clip
description: "Cut a clip out of a video file between timestamps."
io:
  inputs: [file:video]
  outputs: [file:video]
  effects: [read-workspace, write-workspace, exec]
---

# video-clip

Cut a clip out of a video file between timestamps.
