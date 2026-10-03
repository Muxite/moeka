---
name: photo-exif
description: "Show or strip EXIF metadata (camera, GPS location) of photos."
io:
  inputs: [file:image]
  outputs: [data/json]
  effects: [read-workspace, write-workspace]
---

# photo-exif

Show or strip EXIF metadata (camera, GPS location) of photos.
