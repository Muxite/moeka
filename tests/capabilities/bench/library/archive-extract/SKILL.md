---
name: archive-extract
description: "Extract a zip, tar or 7z archive into a folder."
io:
  inputs: [file:archive]
  outputs: [path/dir]
  effects: [read-workspace, write-workspace, exec]
---

# archive-extract

Extract a zip, tar or 7z archive into a folder.
