---
name: web-archive
description: "Save a snapshot of a web page as PDF for archiving."
io:
  inputs: [url/web]
  outputs: [file:pdf]
  effects: [network, write-workspace]
---

# web-archive

Save a snapshot of a web page as PDF for archiving.
