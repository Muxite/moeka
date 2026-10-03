# Feature Specification: Typed capability library and deterministic-first selection

**Feature Branch**: `feat/011-capability-library` (base: `consolidate/new-main`)

**Created**: 2026-10-03

**Status**: Draft (implementation-ready; held-out pattern)

**Input**: Owner plan `plan-further-work-on-melodic-sunbeam.md` Phase 3. Today every skill is listed in the system
prompt on every turn (`nanobot/agent/context.py` `build_system_prompt` → `SkillsLoader.build_skills_summary`), and
the vector skill index (`nanobot/core/vec_store.py` `upsert_skills` / `search_skills`) is never called. Skills
carry no input/output types. As moeka (and later the RSI loop) authors more capabilities, the listing grows without
bound. This spec adds typed I/O manifests, a SQLite capability index, deterministic task typing, and a selector that
**filters by types deterministically before ranking by similarity**. Certificates and script tools are spec 012.

**Held-out pattern** (`004` FR-016): a tester writes hidden tests from this spec only; a different implementer builds
from it and never sees them. Hidden tests use only the [Test Harness Contract](#test-harness-contract).

## Terms

- **Capability**: a skill (any source `SkillsLoader` lists) or a registered tool. Identified by `(kind, name)`,
  `kind ∈ {"skill","tool"}`.
- **Type**: a string from the closed vocabulary in `nanobot/capabilities/types.yaml` (FR-001). Written
  `family[/sub][:variant]`, e.g. `file:pdf`, `text/markdown`, `data/csv`.
- **Ancestor**: `a` is an ancestor of `t` (`t ⊑ a`) when `a` equals `t` or appears on `t`'s parent chain in the
  vocabulary. `any` is the ancestor of every type.
- **Ambient types**: types every task has without extraction: `text/query` always, plus the configured
  `capabilities.ambient_types` (default `["workspace"]`; the gateway adds `"host"`).
- **Task types**: ambient types ∪ the types extracted from the turn by FR-005.
- **Requested output**: a single type extracted by FR-005, or `None` (unconstrained).
- **Untyped capability**: one with no `io` block. It always passes the type filter (FR-007), so adding this feature
  never hides an existing skill.

## Requirements

### Part A — Vocabulary and manifests

- **FR-001 Vocabulary.** `nanobot/capabilities/types.yaml` defines a tree with at least these types (parent in
  brackets): `any`; `text [any]`, `text/query [text]`, `text/markdown [text]`, `text/code [text]`,
  `text/code:python [text/code]`, `text/code:shell [text/code]`, `text/log [text]`; `data [any]`,
  `data/json [data]`, `data/csv [data]`, `data/yaml [data]`; `file [any]`, `file:pdf [file]`,
  `file:image [file]`, `file:audio [file]`, `file:video [file]`, `file:archive [file]`, `file:office [file]`;
  `url [any]`, `url/web [url]`, `url/git [url]`, `url/video [url]`; `path [any]`, `path/dir [path]`,
  `repo:git [path/dir]`; `workspace [any]`; `host [any]`; `none` (no parent, output-only: "effects only").
  `nanobot.capabilities.types` exposes `load_vocabulary()`, `is_type(s) -> bool`, `ancestors(t) -> tuple[str, ...]`
  (self first, `any` last; `none` → `("none",)`), and `satisfies(task_type, required) -> bool` (`required` is an
  ancestor of `task_type`). Unknown strings raise `UnknownTypeError` from `ancestors`/`satisfies`.
- **FR-002 Manifest.** A skill's YAML frontmatter may carry:
  ```yaml
  io:
    inputs: [file:pdf]            # shorthand for {all: [...]}
    # or inputs: {all: [path], any: [file:pdf, file:office]}
    outputs: [text/markdown]
    effects: [read-workspace]
  ```
  `effects` values: `none`, `read-workspace`, `write-workspace`, `network`, `exec`, `sudo`. `sudo` implies `exec`
  for policy checks. Missing `outputs` = `[any]`; missing `effects` = `[exec]` (conservative); missing `inputs` =
  `{all: [], any: []}`. A tool declares the same block as a class attribute `io: dict | None = None` on
  `nanobot.agent.tools.base.Tool`. `nanobot.capabilities.manifest.parse_io(raw) -> IOSpec | None` normalises the
  block (frozen dataclass: `inputs_all`, `inputs_any`, `outputs`, `effects` as sorted tuples) and raises
  `ManifestError` (message names the bad field) for: unknown type, `none` in inputs, unknown effect, non-list
  values, duplicate entries.
- **FR-003 Workspace validation.** `nanobot.capabilities.manifest.validate_skill(path) -> list[str]` returns
  problems (empty = valid) for a `SKILL.md`: the `valid_skill_metadata` identity rules (`nanobot/agent/skills.py`)
  plus a parseable `io` block when present. `SkillsLoader` skips (does not list, does not load) a **workspace**
  skill whose problems are non-empty and logs one warning per skill naming it and the first problem. Built-in skills
  are validated by a test (FR-016), not at runtime.

### Part B — Index

- **FR-004 Index.** `nanobot.capabilities.index.CapabilityIndex(db_path)` keeps a SQLite database (WAL) with the
  capability rows, one row per required input, per any-of input, per output, per effect, a closure table
  `(type, ancestor)` built from the vocabulary, and a `content_sha256` per capability (sha256 of the SKILL.md bytes,
  or of the tool's `name + description + json(parameters) + json(io)`).
  - `sync(entries)` takes the full current list of `CapabilityEntry(kind, name, description, io, content_sha256,
    available)` and makes the index equal to it: inserts new, updates changed (by `content_sha256` or
    `available`), deletes missing, leaves unchanged rows untouched. It returns `SyncResult(added, updated,
    removed, unchanged)` counts.
  - **Invariant**: after any sequence of `sync` calls, the index content equals a fresh index synced once with the
    last list (property-tested).
  - The default path is `<state data_dir>/capabilities.db`; never inside `<workspace>/skills/`.

### Part C — Task typing

- **FR-005 Deterministic extraction.** `nanobot.capabilities.task_types.extract(text, attachments=()) ->
  TaskTyping(types: frozenset[str], requested_output: str | None)`; no model call, no network, no filesystem
  access, pure function of its arguments. Rules (case-insensitive where it says so):
  - Attachments (objects with `.mime` and/or `.name`): `application/pdf`/`.pdf` → `file:pdf`; `image/*` or
    `.png .jpg .jpeg .gif .webp` → `file:image`; `audio/*` or `.mp3 .wav .ogg .m4a .flac` → `file:audio`; `video/*`
    or `.mp4 .mkv .mov .webm` → `file:video`; `.zip .tar .gz .tgz .7z` → `file:archive`; `.docx .xlsx .pptx .doc
    .xls .ppt .odt` → `file:office`; `.csv` → `data/csv`; `.json` → `data/json`; `.yaml .yml` → `data/yaml`;
    `.md` → `text/markdown`; `.py` → `text/code:python`; `.sh` → `text/code:shell`; `.log` → `text/log`.
  - URLs in text (`https?://…`): `github.com`, `gitlab.com`, `bitbucket.org`, or ending `.git` → `url/git`;
    `youtube.com`, `youtu.be`, `vimeo.com` → `url/video`; a URL whose path ends in a file extension above also adds
    that extension's type; otherwise `url/web`.
  - Absolute or `~/` paths in text: a path with an extension above → that type plus `path`; otherwise `path`.
  - Fenced code blocks: ```` ```python ```` → `text/code:python`, ```` ```bash|sh|shell ```` → `text/code:shell`,
    ```` ```json ```` → `data/json`, other or no language → `text/code`.
  - Explicit hints `$type:<type>` in the text add `<type>` if it is in the vocabulary (else ignored).
  - Requested output: the last match in the text of `(as|to|into|in) (a )?(csv|json|yaml|markdown|pdf|table|
    chart|image)` (case-insensitive) maps `csv→data/csv`, `json→data/json`, `yaml→data/yaml`,
    `markdown→text/markdown`, `pdf→file:pdf`, `table→data/csv`, `chart→file:image`, `image→file:image`; else
    `None`. `$out:<type>` overrides when valid.
  - Ambient types are NOT added by `extract` (the selector adds them).

### Part D — Selection

- **FR-006 Selector.** `nanobot.capabilities.select.select(index, typing, query, *, ambient, allowed_effects, k,
  ranker=None) -> Selection` with fields `mode` (`"filtered"` or `"all"`), `survivors: tuple[str, ...]` (names, all
  that passed the filter, ranked), `selected: tuple[str, ...]` (the first `k` survivors), `fallback: str | None`
  (`None`, `"empty"` or `"small"`), `task_types: frozenset[str]`.
- **FR-007 Filter (in SQL against the index).** A capability survives iff all of:
  1. `available` is true (requirements met), **or** it is a skill (unavailable skills keep today's behaviour of
     being listed with an "(unavailable: …)" marker);
  2. every effect `e` is in `allowed_effects` (with `sudo` requiring both `sudo` and `exec`);
  3. untyped, **or**: every type in `inputs_all` is satisfied by some task type (`satisfies(t, required)`), and
     `inputs_any` is empty or one of its types is satisfied by some task type;
  4. untyped, or `requested_output` is `None`, or some output `o` is comparable with it (`satisfies(o, R)` or
     `satisfies(R, o)`), or `o == "any"`.
  **Soundness invariant** (property-tested over random vocab-valid manifests and task types): the SQL filter's
  survivor set equals a straightforward Python evaluation of rules 1-4.
- **FR-008 Ranking.** Survivors are ordered by descending score, ties by `(kind, name)` ascending. The score
  comes from `ranker(query, survivors) -> dict[name, float]` when given. The default ranker uses `VecStore`
  embeddings when the vec store is available (cosine similarity of the query with `"{name}: {description}"`,
  embeddings cached by `content_sha256`). Otherwise it uses a **deterministic lexical scorer**: lowercase
  alphanumeric tokens of the query vs. the capability's name (split on `-`/`_`) and description; score = sum
  over shared tokens of `1/log2(2 + df)`, where `df` is the number of survivors containing the token. Same inputs
  → identical output, across processes.
- **FR-009 Fallbacks.** If no capability survives → `fallback="empty"` and `selected` = every capability (today's
  full listing, in today's order). If the total number of capabilities is `<= k` → `fallback="small"` and
  `selected` = every capability (no filtering, no reordering). `mode="filtered"` in both cases (the fallback field
  says why).
- **FR-010 Prompt integration.** Config `capabilities.selection: "all" | "filtered"` (default `"all"`),
  `capabilities.top_k: int = 8` (1-64), `capabilities.ambient_types: list[str]`,
  `capabilities.allowed_effects: list[str]` (default all six). With `"all"`, the system prompt is **byte-identical**
  to today's. With `"filtered"`, `ContextBuilder.build_system_prompt` passes the turn's text (and attachments when
  the turn has them) through `extract` + `select`, and the skills summary lists only `selected` skills (plus,
  unchanged, `always` skills under "# Active Skills" and explicitly invoked `$skill` skills, which bypass the
  filter and are not counted toward `k`). The summary's line format is unchanged. Tools are not removed in this
  spec (tool schemas are unaffected).
- **FR-011 Trace.** Each filtered build emits a `capability.selected` trace event:
  `{"mode", "fallback", "task_types": sorted list, "requested_output", "survivors": count, "selected": names}`,
  emitted alongside the existing `skill.listed` event, and only when the trace is on, as today.
- **FR-012 Fingerprint.** The `capabilities` config block (selection, top_k, ambient_types, allowed_effects) is part
  of the kernel's run fingerprint, so a variant that changes selection does not reuse a cached result.

### Part E — Library operations

- **FR-013 CLI.** `nanobot capabilities list [--json]` prints every capability with kind, name, io (or `untyped`),
  available, `content_sha256[:12]`. `nanobot capabilities explain "<message>" [--attach FILE ...] [--k N]` prints
  the task types, requested output, survivors with scores, the selection and the fallback, using the active
  instance's workspace (`--workspace` honoured) and without calling a model. `nanobot capabilities validate`
  runs FR-003 over all workspace skills and exits 1 if any is invalid, listing each.
- **FR-014 Built-in annotations.** Every bundled skill under `nanobot/skills/` gets an `io` block. Built-in tools
  that read or write files, exec, or reach the network declare `io.effects` truthfully (`exec` for the shell tool,
  `network` for web tools, `read-workspace`/`write-workspace` for filesystem tools).
- **FR-015 Index wiring.** The index is synced from the live skill and tool lists when a loop builds its first
  context and whenever the skills list changes (cheap: `sync` touches only changed rows). Failure to open or sync the
  index logs once and degrades to `selection="all"` behaviour for that loop (never breaks a turn).

### Non-functional

- **NFR-001 Speed.** The filter (FR-007) over an index of 1000 typed capabilities and 10 task types runs in p99
  < 5 ms on jifan (measured by a benchmark test marked `bench`, excluded from the default run).
- **NFR-002 Quality.** A labelled benchmark (`tests/capabilities/bench/cases.jsonl`, ≥150 cases: message,
  attachments, expected capability names) built from synthetic and real moeka-style tasks over a fixture library of
  ≥100 capabilities: with the lexical ranker, recall@8 (fraction of cases whose expected names are all in
  `selected`) ≥ 0.95, and the skills-summary token count is ≥ 50% lower than `"all"` on average.
- **NFR-003** No new runtime dependency (PyYAML and SQLite exist; vec ranking reuses the optional vec extra).
- **NFR-004** The import boundary holds: `nanobot/capabilities` is kernel code (imports no host package);
  `tests/core/test_import_boundary.py` and `tests/kernel/test_no_ambient_reads.py` pass.

## Test Harness Contract

Hidden tests may use only:
- `nanobot.capabilities.types`: `load_vocabulary`, `is_type`, `ancestors`, `satisfies`, `UnknownTypeError`.
- `nanobot.capabilities.manifest`: `parse_io`, `IOSpec`, `ManifestError`, `validate_skill`.
- `nanobot.capabilities.index`: `CapabilityIndex`, `CapabilityEntry`, `SyncResult`.
- `nanobot.capabilities.task_types`: `extract`, `TaskTyping`.
- `nanobot.capabilities.select`: `select`, `Selection`.
- `nanobot.agent.skills.SkillsLoader` over a temp workspace; `nanobot.agent.context.ContextBuilder` /
  `build_system_prompt` as existing tests construct it (see `tests/agent/`); `nanobot.config.schema.Config`.
- Trace events through the kernel's in-memory trace sink as existing kernel tests use it.
- The CLI via `python -m nanobot capabilities ...` with `--workspace <tmp>`.
- No network, no model calls, no vec extra (hidden tests must pass with the vec store unavailable).

## Success Criteria

- **SC-001** `selection="all"` changes nothing: the system prompt for a fixture workspace is byte-identical before
  and after this feature.
- **SC-002** On a library of 120 capabilities, a turn with a PDF attachment and "summarise this as markdown" lists
  ≤ 8 skills, including the fixture's PDF summariser, and excludes capabilities that require `url/git`.
- **SC-003** The soundness and incremental-sync invariants hold under property tests (≥ 500 examples each).
- **SC-004** NFR-001 and NFR-002 targets met.
