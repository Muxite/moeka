# Plan: capability provenance, certificates and script tools (spec 012)

## Module layout

- `schemas/capability-cert.v1.schema.json`: the `cert` object (FR-001); `schemas/validate.py` accepts the
  `capability-cert` name.
- `nanobot/capabilities/` (kernel code, imports no host package; probed by `tests/core/test_import_boundary.py`)
  - `provenance.py`: `canonical_json`, `bundle_sha256`, `BundleError`, `cert_id_of`, `key_id_of`,
    `cert_schema_problems` (stdlib mirror of the JSON Schema, so `jsonschema` is not a runtime dependency),
    `TrustStore` (`load`, `from_keys`), `Verdict`, `verify_bundle`, `REASONS`, `bundle_stamp`, `trust_stamp`.
  - `trust/__init__.py`: the package trust dir; ships empty (the `__init__.py` makes it ship in the wheel).
  - `enforce.py`: `CertGate` (verdict cache keyed by a stat stamp of the bundle tree and the trust store),
    `Provenance`, `status_for`, once-per-process refusal logging, `capabilities_settings`.
  - `script_tool.py`: `load_manifest`, `ScriptManifest`, `ScriptTool`, `bwrap_available`,
    `scan_script_bundles`, `build_script_tools`.
- Wiring: `CapabilitiesConfig.enforce_certs/trust_dirs/certified_boost` and `ToolsConfig.script`
  (`nanobot/config/schema.py`); `SkillsLoader(capabilities=..., trust=...)` and `ContextBuilder` passing its
  `capabilities` block on; `CapabilityEntry/IndexedCapability.cert_id/cert_status` (index schema v2);
  `select(..., certified_boost=)`; `ToolLoader.load` → `_load_script_tools`; `ToolContext.capabilities`;
  `ToolResult.metadata`; `ProtectedFloor(trust_dirs=)` fed from the env config by the file tools and the gate;
  `nanobot capabilities verify|hash` and the `cert_status` column of `list`; `script_tool.call` in `EVENTS`.

## Verification order

```mermaid
flowchart LR
    A[CERT.json exists] --> B[JSON object] --> C[schema + sig is str] --> D[key id trusted]
    D --> E[Ed25519 signature] --> F[name == dir] --> G[kind == SKILL.md/tool.yaml]
    G --> H[scope == capabilities] --> I[hashable: else bad-bundle] --> J[bundle hash matches]
    J --> K[io == own io] --> L[not revoked]
```

Coordinator clarification: a bundle that raises `BundleError` while hashing (symlink, fifo, socket) is
`bad-bundle`, reported at the hash step, so before `hash-mismatch`, `io-mismatch` and `revoked`; a symlinked
bundle with an otherwise valid certificate reports `bad-bundle`.

## Decisions on spec ambiguities

- **`Verdict` on success**: `reason == "ok"`. `cert_id` is `None` for `no-cert`, `bad-json` and `bad-schema` and set
  for every later reason (the certificate parsed).
- **What is `bad-json` vs `bad-schema`**: unreadable or non-UTF-8 file, invalid JSON, or a top-level value that is not
  an object → `bad-json`. A missing `cert`, a non-string `sig`, or a `cert` failing the v1 schema → `bad-schema`.
  Extra top-level keys next to `cert`/`sig` are tolerated. A `sig` that is not base64 or not 64 bytes is
  `bad-signature` (it is checked after the key lookup).
- **Only the root `CERT.json` is excluded** from the bundle hash; a nested `CERT.json` is an ordinary file. The bundle
  root itself may be reached through a symlink; a symlink inside it is a `BundleError`.
- **Kind of a bundle**: `SKILL.md` → `skill`, `tool.yaml` → `script-tool`; with both, the parent directory name
  decides (`tools-script` → script tool), else skill.
- **`io-mismatch`**: the bundle's own `io` (skill frontmatter or `tool.yaml`) normalised with `parse_io` and compared
  list by list with the (sorted) cert `io`. An absent or unparseable block never matches.
- **Trust store**: `TrustStore.load(dirs)` also reads the package trust dir (`include_builtin=True`); a missing dir is
  skipped silently, a malformed `.pub` file or `revoked.jsonl` line is skipped with a warning. `.pub` files must be a
  single line of standard base64 decoding to a valid 32-byte Ed25519 key.
- **Status strings**: `certified`; `uncertified` (no cert, enforcement off); `refused:<reason>` (any other verdict,
  and `no-cert` when enforcing); `builtin` for built-in, inline and plugin skills and built-in tools. With
  enforcement off a `refused:<reason>` bundle still loads (status only).
- **Where enforcement settings come from**: an explicit `capabilities=` argument (SkillsLoader, ContextBuilder,
  `ToolContext.capabilities`), else the env's `capabilities` config section, else off. `SkillsLoader(trust=...)`
  pins a `TrustStore` (embedders, tests).
- **Freshness**: verdicts are cached per bundle by a stat stamp (paths, modes, sizes, mtimes, inodes) of the tree
  and the identity of the trust store, which is reloaded when a `.pub` or `revoked.jsonl` changes. A byte flip, a
  rename, an io edit or a revocation is therefore seen by the same loader on its next listing (SC-002), not only by
  a fresh one.
- **A refused workspace skill that shadows a built-in** of the same name: the built-in loads (the workspace entry is
  gone from the listing).
- **Boost scope**: applied only with the default ranker (`ranker=None`, `lexical_ranker`, `vec_ranker`); a custom
  ranker's scores are used as given. `select(..., certified_boost=0.10)` by default; `ContextBuilder` and `explain`
  pass `capabilities.certified_boost`. A zero score stays zero.
- **Script manifest**: `timeout_s` defaults to 30 when absent; `parameters` must be a mapping with `type: object`
  (or no type); unknown manifest keys are tolerated. `entry` must be relative, stay inside the bundle after `..`
  normalisation, pass through no symlink, be a regular file and be executable.
- **Execution**: the child runs in its own session (`start_new_session`), so a timeout `killpg`s the whole group;
  leftovers in the group are also killed when the entry exits (a backgrounded child cannot hold stdout open). Under
  bwrap the sandbox adds `--unshare-pid --die-with-parent`, binds `/usr` (+ `/bin`, `/lib*` when present), the
  bundle read-only, the temp `HOME`, and the workspace (read-only for `read-workspace`, read-write for
  `write-workspace`) only when exported. stderr keeps the last 2000 characters. Errors: `timeout`, stdout over
  1 MiB, non-zero exit (code + stderr tail), non-JSON stdout (exit code + stderr tail), output-schema violation.
- **"bwrap available"** (coordinator clarification: usable, not just installed) means a probe sandbox with `--unshare-net --unshare-pid` actually starts (cached per process):
  Docker and userns-restricted hosts (jifan included) ship a `bwrap` that cannot unshare the network, and there the
  tool runs unsandboxed with `sandboxed: false`, as the spec allows. SC-003's test skips with that reason.
- **Result metadata**: `ToolResult.metadata` (new, optional) carries `tool`, `sandboxed`, `exit_code`, `duration_ms`,
  `effects_declared`; the registry keeps it on error results. `ScriptTool.last_metadata` mirrors the last call.
- **Gate capability**: a script tool declares `exec.run` (resource: the entry path), so a policy that denies exec
  denies script tools too.
- **Registration**: after built-ins and kernel plugins, honouring `tools_allow`/`tools_deny`; a name already taken is
  skipped with a warning. `nanobot capabilities list` shows every workspace bundle, including refused skills and
  script tools that are refused, invalid (`invalid`) or not enabled (`available: false`).
- **Protected paths**: `CERT.json`/`revoked.jsonl` anywhere and every trust dir (package + configured, as given and
  resolved) are write-only-denied (reads allowed), in both the file tools' floor and the kernel gate floor.
- **CLI `verify`**: `--trust-dir` replaces the configured trust dirs; without it the config's `trust_dirs` are used.
  The package trust dir is always read.
- **Dependency**: `cryptography>=44.0.0` is explicit in `dependencies` and in the `core` extra (kernel code uses it);
  `uv.lock` changes only by those requirement lines.
