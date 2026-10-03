"""Typed capability library and deterministic-first selection (spec 011).

- ``types``: the closed type vocabulary (``types.yaml``) and the ancestor relation.
- ``manifest``: ``io`` blocks on skills and tools (``parse_io``, ``validate_skill``).
- ``index``: the SQLite capability index (``CapabilityIndex.sync`` / ``filter``).
- ``task_types``: deterministic extraction of a turn's types (``extract``).
- ``select``: filter by types first, then rank (``select``).
- ``lint``: static lint of skill text (``lint_text``; spec 010 FR-002).
- ``pending``: Dream's staged skills and their accept/reject review (spec 010 FR-001).

Kernel code: imports no host package (channels, gateway, webui, cli, ...).
"""
