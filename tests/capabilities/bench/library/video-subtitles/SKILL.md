---
name: video-subtitles
description: "Generate subtitles (srt) for a video."
io:
  inputs: {all: [], any: [file:video, url/video]}
  outputs: [text]
  effects: [read-workspace, write-workspace, network]
---

# video-subtitles

Generate subtitles (srt) for a video.
