---
name: python-profile
description: "Profile slow Python code and find hot spots."
io:
  inputs: [text/code:python]
  outputs: [text/markdown]
  effects: [read-workspace, exec]
---

# python-profile

Profile slow Python code and find hot spots.
