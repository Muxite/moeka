---
name: web-scrape-table
description: "Scrape an HTML table from a web page into CSV."
io:
  inputs: [url/web]
  outputs: [data/csv]
  effects: [network, write-workspace]
---

# web-scrape-table

Scrape an HTML table from a web page into CSV.
