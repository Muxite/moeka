# Tasks: capability provenance, certificates and script tools (spec 012)

- [x] T01 FR-001 `schemas/capability-cert.v1.schema.json` (+ `validate.py` name); stdlib mirror
      `cert_schema_problems`, agreement test against `jsonschema`
- [x] T02 FR-002 `provenance.py`: canonical JSON, `bundle_sha256` / `BundleError`, `Verdict`, `verify_bundle` with
      the exact reason order (`bad-bundle` at the hash step); one fixture per reason (SC-001) and order tests
- [x] T03 FR-003 `TrustStore.load` (malformed `.pub` / `revoked.jsonl` lines skipped with warnings, duplicate ids)
      and `TrustStore.from_keys`; empty `nanobot/capabilities/trust/` package dir
- [x] T04 FR-004 no private-key code under `nanobot/` (grep test), no `sign` in the public API; signer only in
      `tests/capabilities/_certkit.py`
- [x] T05 FR-005 `capabilities.enforce_certs`, `capabilities.trust_dirs` (`~` expanded),
      `capabilities.certified_boost` (0-1), `tools.script.enabled`
- [x] T06 FR-006 `CertGate` + `SkillsLoader` gate (listing, loading, `$name`), once-per-(name, reason) refusal log,
      built-in/inline/plugin unaffected; SC-002 matrix (flip, rename, io edit, revoke) on the same and a fresh loader
- [x] T07 FR-007 index schema v2 (`cert_id`, `cert_status`), `skill_entries`/`tool_entries` provenance, boost in the
      default ranker, `ContextBuilder`/`explain` pass `certified_boost`
- [x] T08 FR-008 `ProtectedFloor(trust_dirs=)`, `CERT.json`/`revoked.jsonl` write-denied, file tools and gate floor
      read `capabilities.trust_dirs` from the env
- [x] T09 FR-009 `load_manifest` / `ScriptManifest` / `ManifestError` per field (entry escape, symlink, exec bit)
- [x] T10 FR-010 `ScriptTool` execution contract (stdin args, cwd, scrubbed env, `MOEKA_WORKSPACE` by effect,
      process-group timeout, 1 MiB JSON stdout, output schema, stderr tail, bwrap `--unshare-net`, `sandboxed`
      metadata); `ToolLoader` registration as `script_<name>`; `ToolResult.metadata`
- [x] T11 FR-011 `script_tool.call` trace event (+ `EVENTS` entry), no arguments or output
- [x] T12 FR-012 `nanobot capabilities verify|hash`, `list` status column (text and `--json`)
- [x] T13 NFR-001 explicit `cryptography` dependency (`uv lock`); NFR-002 100-file timing test; NFR-003 import
      boundary probe extended
- [x] T14 SC-003 network test (skips with a recorded reason when bwrap cannot unshare the network)
- [x] T15 Docs: core-map doc 03 (provenance, script tools, protected trust paths)
- [x] T16 Targeted Docker suites + ruff; full Docker suite
