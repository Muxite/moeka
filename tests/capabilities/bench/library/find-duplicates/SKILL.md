---
name: find-duplicates
description: "Find duplicate files under a directory by content hash."
io:
  inputs: [path]
  outputs: [text]
  effects: [read-workspace]
---

# find-duplicates

Find duplicate files under a directory by content hash.
