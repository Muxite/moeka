# Tasks: typed capability library (spec 011)

- [x] T01 FR-001 `types.yaml` vocabulary + `types.py` (`load_vocabulary`, `is_type`, `ancestors`, `satisfies`,
      `UnknownTypeError`); ship the yaml in the wheel (`pyproject.toml` include)
- [x] T02 FR-002 `manifest.parse_io` / `IOSpec` / `ManifestError`; `Tool.io = None` on the tool base class
- [x] T03 FR-003 `manifest.validate_skill`; `SkillsLoader` skips invalid workspace skills (cached per file
      mtime/size, one warning per skill); fix existing tests that wrote non-conforming workspace skills
- [x] T04 FR-004 `CapabilityIndex` (WAL, child tables, closure table, `sync` -> `SyncResult`)
- [x] T05 FR-005 `task_types.extract` (attachments, URLs, paths, fences, `$type:`, requested output, `$out:`)
- [x] T06 FR-006/007 `select` + set-based SQL filter, `kinds` and `exclude` options
- [x] T07 FR-008 lexical ranker (deterministic, cross-process test), vec ranker via
      `VecStore.rank_capabilities` with embeddings cached by `content_sha256`; legacy skill methods kept
- [x] T08 FR-009 fallbacks `empty` / `small`
- [x] T09 FR-010 `CapabilitiesConfig` (`Config.capabilities`, camelCase aliases, validation); `ContextBuilder`
      filtered listing, attachments from the turn's media, `$skill` bypass, always-on skills excluded from k;
      byte-identical `"all"` (golden from the pre-011 code)
- [x] T10 FR-011 `capability.selected` trace event (+ `EVENTS` catalogue entry)
- [x] T11 FR-012 non-default `capabilities` block folded into the fingerprint's `system_prompt` component
- [x] T12 FR-013 `nanobot capabilities list [--json] | explain [--attach] [--k] [--json] | validate`
- [x] T13 FR-014 `io` blocks on every bundled skill; truthful `io.effects` on file, exec and web tools;
      `quick_validate.py` accepts `io`; skill-creator documents it
- [x] T14 FR-015 index synced from live skill + tool lists (signature check), graceful degradation
- [x] T15 Property tests: SQL filter == Python reference (600 examples); incremental sync == fresh (500)
- [x] T16 NFR-001/002 benchmark: `tests/capabilities/bench/` (124-capability library, 163 cases), quality test
      in the default run, latency test marked `bench` (skipped unless `-m bench`)
- [x] T17 NFR-004 import boundary probe extended; `test_no_ambient_reads` passes
- [x] T18 Docs: core-map README + doc 03 (loader validation, `io` block, selection config)
- [x] T19 Targeted Docker suites + ruff; full Docker suite
