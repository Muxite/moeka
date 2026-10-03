---
name: docker-cleanup
description: "Prune unused docker images, containers and volumes on this host."
io:
  inputs: [host]
  outputs: [text]
  effects: [exec]
---

# docker-cleanup

Prune unused docker images, containers and volumes on this host.
