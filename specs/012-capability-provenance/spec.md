# Feature Specification: Capability provenance, certificates and script tools

**Feature Branch**: `feat/012-capability-provenance` (base: `integrate/009-011`)

**Created**: 2026-10-03

**Status**: Draft (implementation-ready; held-out pattern)

**Input**: Owner plan `plan-further-work-on-melodic-sunbeam.md` Phases 3 and 5. The RSI harness (moeka-rsi) will
author capabilities inside a VM and certify them with an Ed25519 key that only its host-side verifier (L0) holds.
Moeka must be able to **verify** those certificates (public key only), refuse uncertified or revoked workspace
capabilities when enforcement is on, and run a new kind of capability, the **script tool**, that an author agent can
write without changing kernel Python code. Moeka never signs: it has no code path that reads a private key.

**Held-out pattern** (`004` FR-016). Hidden tests use only the [Test Harness Contract](#test-harness-contract).

## Terms

- **Bundle**: a capability directory: `<workspace>/skills/<name>/` (contains `SKILL.md`) or
  `<workspace>/tools-script/<name>/` (contains `tool.yaml`). `CERT.json` inside it is not part of the bundle.
- **Bundle hash**: `sha256` hex of the UTF-8 bytes of the canonical JSON array `[[relpath, sha256hex(file bytes)],
  ...]` over every regular file in the bundle except `CERT.json`, `relpath` POSIX-style relative to the bundle root,
  sorted by `relpath`. A bundle containing a symlink, device, fifo or socket has **no** valid hash (hashing raises
  `BundleError`).
- **Canonical JSON**: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.
- **Certificate**: the object in `CERT.json`'s `"cert"` field; `"sig"` holds the base64 (standard alphabet, padded)
  Ed25519 signature over the canonical JSON of `"cert"`.
- **Cert id**: the first 16 hex characters of `sha256(canonical JSON of cert)`.
- **Key id**: the first 16 hex characters of `sha256(raw 32-byte public key)`.
- **Trust store**: a directory holding `*.pub` files (each a single line: the base64 raw 32-byte Ed25519 public key)
  and an optional `revoked.jsonl`. Default: the package's `nanobot/capabilities/trust/` (ships empty) plus the
  directories in `capabilities.trust_dirs` (config).

## Requirements

### Part A — Certificates

- **FR-001 Certificate schema.** `schemas/capability-cert.v1.schema.json` defines `cert`: `schema` =
  `"moeka.capcert.v1"`, `kind` ∈ {`skill`, `script-tool`}, `name` (the bundle's directory name), `bundle_sha256`
  (64 hex), `io` (the normalised IOSpec as `{inputs_all, inputs_any, outputs, effects}` lists), `scores` (object,
  free-form numbers), `lineage` (`{"parent": cert id | null}`), `verifier` (`{"version": str, "suite": str}`),
  `issued_at` (RFC 3339 UTC), `key_id` (16 hex), `scope` ∈ {`capabilities`, `kernel`} (`kernel` = a kernel-scope patch cert from the
  RSI loop, never loadable as a capability). All required;
  no extra properties.
- **FR-002 Verify.** `nanobot.capabilities.provenance.verify_bundle(bundle_dir, trust) -> Verdict` where
  `Verdict(ok: bool, reason: str, cert_id: str | None)`. `ok` is true only if all hold, else `reason` is exactly one of
  the codes: `no-cert`, `bad-json`, `bad-schema`, `unknown-key` (no trusted key with `key_id`), `bad-signature`,
  `name-mismatch`, `kind-mismatch`, `scope-mismatch` (cert `scope` is not `capabilities`), `hash-mismatch` (recomputed bundle hash ≠ `bundle_sha256`), `io-mismatch` (the
  bundle's own `io` block, normalised, ≠ `cert.io`), `revoked`, `bad-bundle` (BundleError). The checks run in the
  order listed, and the first failure is the reason.
- **FR-003 Trust store.** `nanobot.capabilities.provenance.TrustStore.load(dirs) -> TrustStore` reads public keys and
  revocations; malformed `.pub` files are skipped with a warning; duplicate key ids are fine (same key).
  `revoked.jsonl` lines are `{"cert_id": ..., "reason": ..., "at": ...}`; a line that is not valid JSON is skipped
  with a warning. `TrustStore.from_keys(pubkeys: list[bytes], revoked: set[str] = ())` exists for embedders/tests.
- **FR-004 No signing in moeka.** No module under `nanobot/` or `moeka/` loads, accepts or generates an Ed25519
  **private** key (enforced by a test that greps for `Ed25519PrivateKey`, `from_private_bytes`,
  `load_pem_private_key`, `generate(` in `nanobot/capabilities` and asserts none, and that the public API has no
  `sign` function). Test fixtures may sign.

### Part B — Enforcement

- **FR-005 Config.** `capabilities.enforce_certs: bool = False`, `capabilities.trust_dirs: list[str] = []`
  (`~` expanded). `tools.script.enabled: bool = False`.
- **FR-006 Loader gate.** With `enforce_certs = true`:
  - Workspace skills load (are listed, loadable, invocable with `$name`) only when `verify_bundle` is `ok`.
  - Script tools register only when `ok`.
  - Built-in skills, inline skills, and plugin skills (already hash-gated by the plugin registry) are unaffected.
  - A refused capability is logged once per process per `(name, reason)` and appears in
    `nanobot capabilities list` with status `refused:<reason>`.
  With `enforce_certs = false` nothing is refused for lack of a cert (today's behaviour), but `capabilities list`
  still shows `certified`, `uncertified`, or `refused:<reason>` for every workspace bundle.
- **FR-007 Index provenance.** The capability index (spec 011) stores per capability `cert_id` (or null) and
  `cert_status` (`certified`, `uncertified`, `refused:<reason>`, `builtin`). The default ranker multiplies a
  certified capability's score by `1 + capabilities.certified_boost` (default `0.10`, range 0-1) before ordering.
- **FR-008 Protected trust paths.** `nanobot/security/protected_paths.py` denies **write** through the file tools to
  any path inside a configured trust dir and to any file named `CERT.json` or `revoked.jsonl`. (Stated limit: an
  agent with unrestricted exec can still write these; real protection for the trust root is the OS boundary, i.e.
  ownership by another uid. This requirement prevents accidental writes through the file tools.)

### Part C — Script tools

- **FR-009 Manifest.** `<workspace>/tools-script/<name>/tool.yaml`:
  ```yaml
  name: <name>                 # == directory name, skill naming rule
  description: <1-1024 chars>
  parameters: {JSON Schema object}
  output_schema: {JSON Schema}  # optional
  io: {inputs, outputs, effects}   # spec 011 block, required
  entry: run                   # relative path to an executable inside the bundle
  timeout_s: 30                # 1-600
  ```
  `nanobot.capabilities.script_tool.load_manifest(bundle_dir) -> ScriptManifest` validates and raises
  `ManifestError` naming the field for: missing or invalid fields, an `entry` that escapes the bundle (after
  resolving `..`), is a symlink, or is not executable.
- **FR-010 Execution.** `ScriptTool` (a `Tool` subclass) is registered as `script_<name>` when
  `tools.script.enabled` and (if enforcing) certified. A call:
  - validates arguments against `parameters` (the existing tool param validation);
  - runs `entry` with cwd = the bundle dir, stdin = canonical JSON of the arguments, an environment containing only
    `PATH=/usr/bin:/bin`, `HOME=<a fresh temp dir>`, `LANG=C.UTF-8`, and `MOEKA_WORKSPACE` only if `io.effects`
    includes `read-workspace` or `write-workspace`;
  - is killed (whole process group) after `timeout_s`, returning an error result `timeout`;
  - expects stdout to be one JSON value (≤ 1 MiB), validated against `output_schema` when present (existing
    output-schema check). Non-JSON stdout or a non-zero exit returns an error result with the exit code and the
    last 2000 characters of stderr;
  - when `io.effects` lacks `network` and the shell tool's bwrap backend is available, runs under bwrap with
    `--unshare-net`. When bwrap is unavailable, a manifest without `network` still runs, and the tool's result
    metadata records `sandboxed: false`.
- **FR-011 Effects honesty hook.** `ScriptTool` records per call `{"tool", "effects_declared", "sandboxed",
  "exit_code", "duration_ms"}` as a `script_tool.call` trace event (no arguments, no output).

### Part D — Operations

- **FR-012 CLI.**
  - `nanobot capabilities verify <bundle_dir> [--trust-dir D ...]` prints `ok <cert_id>` (exit 0) or
    `refused <reason>` (exit 1).
  - `nanobot capabilities hash <bundle_dir>` prints the bundle hash.
  - `nanobot capabilities list` gains the status column of FR-006.

### Non-functional

- **NFR-001** Uses `cryptography` (already in `uv.lock`); make it an explicit dependency in `pyproject.toml` if it is
  only transitive today.
- **NFR-002** Verification of a 100-file bundle completes in < 50 ms.
- **NFR-003** Kernel import boundary holds (`nanobot/capabilities` imports no host package).

## Test Harness Contract

Hidden tests may use only:
- `nanobot.capabilities.provenance`: `verify_bundle`, `Verdict`, `TrustStore`, `bundle_sha256`, `BundleError`.
- `nanobot.capabilities.script_tool`: `load_manifest`, `ScriptManifest`, `ScriptTool`.
- `nanobot.capabilities.manifest.ManifestError`; `nanobot.capabilities.index.CapabilityIndex` (spec 011 surfaces).
- `SkillsLoader`, `ToolLoader`/`ToolRegistry` as existing tests construct them; `Config`; the kernel trace sink.
- `schemas/capability-cert.v1.schema.json` with `schemas/validate.py`.
- The CLI via `python -m nanobot capabilities ...`.
- `cryptography.hazmat.primitives.asymmetric.ed25519` in the **tests** to create keys and sign fixtures.

## Success Criteria

- **SC-001** Every `reason` code in FR-002 is produced by a fixture built for it, and only by it.
- **SC-002** With enforcement on, flipping one byte of any bundle file, renaming the dir, editing `io`, or revoking
  the cert removes the capability from the prompt summary and the tool registry on the next load.
- **SC-003** A script tool without `network` cannot open a TCP connection when bwrap is present, and that test is
  skipped with a recorded reason when bwrap is absent.
- **SC-004** No private-key handling exists in moeka source (FR-004).

## Amendment 2026-10-03 (head clarifications)

- **FR-010 "bwrap available" means usable.** Probe once per process by running a trivial
  `bwrap --unshare-net … true`, and cache the result. On hosts where unprivileged user namespaces are restricted
  (jifan: AppArmor; `--unshare-net` fails with "loopback: Failed RTM_NEWADDR"), the probe fails. The tool then runs
  unsandboxed with `sandboxed: false` in its result metadata and trace event. Network containment for RSI-authored
  tools comes from L0's docker `--network none` verification (moeka-rsi spec 004) and from certificate gating,
  not from this best-effort sandbox.
- **FR-002 `bad-bundle` position.** An unhashable bundle (symlink, fifo, socket) gives `bad-bundle` at the point
  where the bundle hash is computed, that is, before `hash-mismatch`, `io-mismatch` and `revoked`.
