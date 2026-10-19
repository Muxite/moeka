---
name: notes
description: Keep concise project notes and durable user facts with searchable sources.
io:
  inputs: [text/query]
  outputs: [text/markdown]
  effects: [read-workspace, write-workspace]
---

# Notes

Use the `memory` tool to remember, search, correct, and forget durable user facts; keep
its returned IDs and source references intact. Put project-specific working notes in a
clearly named Markdown file under the workspace. Search before adding a note, update
the existing section when one already covers the topic, and keep notes factual and
short. Do not store credentials, private keys, or sensitive personal details.
