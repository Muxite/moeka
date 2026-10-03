---
name: archive-create
description: "Pack a folder into a zip or tar archive."
io:
  inputs: [path]
  outputs: [file:archive]
  effects: [read-workspace, write-workspace, exec]
---

# archive-create

Pack a folder into a zip or tar archive.
