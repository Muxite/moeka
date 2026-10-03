---
name: youtube-download
description: "Download a YouTube or Vimeo video for offline viewing."
io:
  inputs: [url/video]
  outputs: [file:video]
  effects: [network, write-workspace, exec]
---

# youtube-download

Download a YouTube or Vimeo video for offline viewing.
