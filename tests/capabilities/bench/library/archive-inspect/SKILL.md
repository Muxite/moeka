---
name: archive-inspect
description: "List the contents of an archive without extracting it."
io:
  inputs: [file:archive]
  outputs: [text]
  effects: [read-workspace, exec]
---

# archive-inspect

List the contents of an archive without extracting it.
