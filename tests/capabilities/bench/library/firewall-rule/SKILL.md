---
name: firewall-rule
description: "Add or remove ufw firewall rules on this host."
io:
  inputs: [host]
  outputs: [none]
  effects: [sudo, exec]
---

# firewall-rule

Add or remove ufw firewall rules on this host.
