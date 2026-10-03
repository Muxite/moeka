---
name: video-compress
description: "Re-encode a video file to a smaller size."
io:
  inputs: [file:video]
  outputs: [file:video]
  effects: [read-workspace, write-workspace, exec]
---

# video-compress

Re-encode a video file to a smaller size.
