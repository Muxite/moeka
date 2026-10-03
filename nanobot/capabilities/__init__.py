"""Typed capability library and deterministic-first selection (spec 011).

- ``types``: the closed type vocabulary (``types.yaml``) and the ancestor relation.
- ``manifest``: ``io`` blocks on skills and tools (``parse_io``, ``validate_skill``).
- ``index``: the SQLite capability index (``CapabilityIndex.sync`` / ``filter``).
- ``task_types``: deterministic extraction of a turn's types (``extract``).
- ``select``: filter by types first, then rank (``select``).

Kernel code: imports no host package (channels, gateway, webui, cli, ...).
"""
