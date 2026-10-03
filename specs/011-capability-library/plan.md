# Plan: typed capability library (spec 011)

## Module layout

- `nanobot/capabilities/` (kernel code, imports no host package; probed by `tests/core/test_import_boundary.py`)
  - `types.yaml` + `types.py`: the vocabulary (`type: parent`), `load_vocabulary` (read-only mapping, validated
    once: parents exist, no cycles, `any`/`none` roots), `is_type`, `ancestors`, `satisfies`, `closure`.
    `types.yaml` ships in the wheel through `[tool.hatch.build] include`.
  - `manifest.py`: `IOSpec`, `parse_io`, `ManifestError` (message starts with the dotted field, e.g.
    `io.inputs.any: unknown type 'x'`), `validate_skill`, `EFFECTS`.
  - `task_types.py`: `extract` (pure regex/table code), `TaskTyping`, `Attachment`.
  - `index.py`: `CapabilityIndex`, `CapabilityEntry`, `SyncResult`, `IndexedCapability`.
  - `select.py`: `select`, `Selection`, `lexical_ranker`, `vec_ranker`, `default_ranker`, `Candidate`.
  - `library.py`: `skill_entries(loader)`, `tool_entries(tools)` (FR-004 sha256 rules), `entries_signature`.
- Wiring: `SkillsLoader._workspace_skill_valid` (FR-003), `Tool.io` (FR-002), `CapabilitiesConfig` at
  `Config.capabilities` (FR-010), `ContextBuilder._select_skills` (FR-010/011/015), `AgentLoop` passes
  `config.capabilities`, the tool registry and the host ambient type, `variants.fingerprint` (FR-012),
  `VecStore.rank_capabilities` / `capability_vectors` (FR-008), `nanobot/cli/capabilities.py` (FR-013).

## Index schema and the filter

Tables: `capabilities(id, kind, name, description, io_json, typed, available, content_sha256, ordinal)` with
`UNIQUE(kind, name)`; `cap_inputs_all`, `cap_inputs_any`, `cap_outputs`, `cap_effects` (`cap_id`, value, `ON
DELETE CASCADE`); `type_closure(type, ancestor)` (self pairs included, rebuilt when the vocabulary changes);
`meta(key, value)`. WAL on disk; `:memory:` supported.

The filter is one statement with uncorrelated subqueries (each evaluated once per query). The task types are
expanded in Python to their ancestor set `T*`, so "required `r` is satisfied by some task type" is `r IN T*`:

```sql
SELECT ... FROM capabilities c
WHERE (c.available = 1 OR c.kind = 'skill')
  AND (:kinds IS NULL OR c.kind IN json_each(:kinds))
  AND c.id NOT IN (SELECT cap_id FROM cap_effects
                   WHERE effect NOT IN json_each(:effects) OR (effect = 'sudo' AND :exec_ok = 0))
  AND (c.typed = 0 OR (
        c.id NOT IN (SELECT cap_id FROM cap_inputs_all WHERE type NOT IN json_each(:task))
    AND (c.id NOT IN (SELECT cap_id FROM cap_inputs_any)
         OR c.id IN (SELECT cap_id FROM cap_inputs_any WHERE type IN json_each(:task)))
    AND (:requested IS NULL OR c.id IN (
          SELECT cap_id FROM cap_outputs WHERE type = 'any'
             OR type IN (SELECT ancestor FROM type_closure WHERE type = :requested)
             OR type IN (SELECT type FROM type_closure WHERE ancestor = :requested)))))
```

Measured (jifan, `-m bench`): 1000 typed capabilities, 10 task types: p50 2.1 ms, p99 3.8 ms.

## Decisions on spec ambiguities

- **Untyped and effects.** An untyped capability has no effect rows, so it passes rule 2 as well as rules 3-4
  ("never hides an existing skill"). A typed block without `effects` gets `[exec]`.
- **Fallback precedence.** `small` (library size <= k) is checked before `empty`; both list every capability in
  sync-list order. `survivors` are always the real filter result, ranked.
- **Which capabilities the prompt selects over.** Tools are indexed (FR-015) but the prompt selects with
  `kinds=("skill",)` so tools never take one of the k skill slots; always-on skills are passed as `exclude`
  (they are not counted toward k). Explicit `$skill` references are added after selection.
- **Config source.** `ContextBuilder(capabilities=...)` wins, else `env.config.section("capabilities")`, else
  the defaults. `AgentLoop.from_config` passes `config.capabilities`. Host loops (`host_tools=True`: gateway,
  serve, CLI agent) add the `host` ambient type; `nanobot capabilities explain` does too.
- **Fingerprint.** The kernel-held held-out suite 006 pins exactly five components and the exact `skills`
  recomputation, so the block is folded into the `system_prompt` component, and only when it differs from the
  defaults (a loop that does not use selection keeps its pre-011 digest).
- **Ranker interface.** Rankers get `Candidate` objects: `str` subclasses (the name) carrying `kind`,
  `description` and `content_sha256`, so `ranker(query, names)` and attribute-based rankers both work. Scores
  are keyed by name. The lexical scorer applies the FR-008 formula literally (no stop words, no stemming).
- **URLs with a file extension.** `https://host/a.pdf` gives `{url/web, file:pdf}`: the extension type is added
  to the URL's own classification (git, video, else web).
- **Fences.** Any triple backtick opens or closes a fence (fences alternate); the language after an opening one
  is matched case-insensitively. `py` is not `python` (spec lists `python` only).
- **Index sharing.** The default file is `<state data_dir>/capabilities.db`; a builder without an env keeps an
  in-memory index. A per-file lock makes each sync-then-filter atomic, and the stored `meta.signature` (not a
  local cache) decides whether a sync is needed, so loops with different skill sets can share the file.
- **Sync change detection.** Any field change (description, io, sha, availability) updates the row; an
  ordinal-only change is applied silently (counted as unchanged). A duplicate `(kind, name)` in one list: the
  last one wins.
- **Degradation.** Any exception while opening, syncing or selecting logs once, switches that builder to
  `"all"` for its lifetime, and emits `capability.selected` with `mode="all"` on later filtered builds.
- **Benchmark.** Ambient types are the gateway's (`workspace`, `host`); tokens are word/punctuation runs (no
  tokenizer download). Results: 163 cases, 124 capabilities, recall@8 0.982, summary tokens -93.4%.
