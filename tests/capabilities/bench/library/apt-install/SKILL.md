---
name: apt-install
description: "Install system packages with apt on this host."
io:
  inputs: [host]
  outputs: [none]
  effects: [sudo, exec]
---

# apt-install

Install system packages with apt on this host.
