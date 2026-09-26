# Phase 0 follow-ups

Deferred minors and known limits recorded while implementing phase 0 (spec: `.agent/host-plugin-permissions-design.md`
section 3; outcome: its subsection "Phase 0 outcome"). "Found" gives the task and commit where the review raised it.
"Suggested" is a proposal for the owner, not a decision. Nothing here is a regression: each item was accepted as
deferred by the reviewer or by a ruling (R1-R3) in the ledger.

## 1. Security limits

| Item | Found | Suggested owner / phase |
|---|---|---|
| Split-write evasion: a pattern split across two `exec_session` writes passes (no per-session buffering); also `s''udo`, multi-line splits and a full-width colon evade it, as they do for exec | T2, f623064 | Accept (guard is a hint, E4); real fix is host-layer isolation (H) |
| Hard links to protected files (incl. `config.json`) evade the path-based file-tool floor; FIFOs not covered | T1 6313a20, T10 fde43f1 | P1 host seams / host mounts |
| TOCTOU symlink swap between resolve and open | T1, 2e93751 | P2 gate or host layer |
| Custom host `sessions_root` not covered by the floor; `_FsTool._protected_floor` and `image_generation` hard-wire `default_data_dir()` | T1, 2e93751 / 6313a20 | P1 `Paths` injection (`ProtectedFloor(data_dir=...)` already accepts one) |
| `exec` reaches every protected path (file-tool floor only); `image_generation` `save_dir` writes are not covered by the floor (theoretical) | T1, T10 | Host-layer isolation (H) and P2 gate |
| `/proc/<pid>/{smaps,smaps_rollup,numa_maps}` still readable (same layout data as `maps`) | T1, 2e93751 | Small fix, next hardening pass |
| Zero-width and bidi format characters (category Cf: U+200B-U+200F, U+202A-U+202E, U+2066-U+2069) not stripped from MCP descriptions; `description` keys nested in `default`/`examples` are also rewritten; a long resource/prompt description can truncate the URI line because the cap is applied after the wrapper's own suffix | T6, 015e27b | Follow-up hardening of `sanitize_description` |
| MCP description cap yields 2001 chars (`[:limit]` plus an ellipsis; pinned by a test) | T6, 015e27b | Doc nit or test change |
| Bocha and Volcengine error strings include external text without the untrusted banner (`web.py`) | T6, 015e27b | Small fix in the search backends |
| Local-use NAT64 `64:ff9b:1::/48` and IPv4-compatible `::a.b.c.d` addresses are not normalised | T7, c5ba1e1 | Small fix in `_normalize_addr` |
| `validate_resolved_url` has no production caller (the fail-closed change is defence in depth only) | T7, c5ba1e1 | P1: use it or delete it |
| Redaction: name part capped at 64 chars, so `'a'*100 + 'key=SECRET'` leaks (unrealistic); benign text can be over-masked (`monkey: banana`, `max_tokens=4096`); other log sites that log tool arguments were not audited (none found by grep) | T5, c4f0cf3 | Accept; revisit with the P2 audit record |
| `grep`: stacked bounded repeats (each max <= 100, ~100^k); single-repeat quadratic patterns on lines under 10 000 chars (bounded only by the cooperative deadline, checked every 64 lines); memory on newline-heavy explicit files (20 MB of `\n` with a routed pattern: 2.7 GB RSS, batches sized by text chars only) | T8, 45aa568 (ruling R3) | Deferred by ruling; revisit if grep is exposed to untrusted paths |
| `grep` screen uses the private `re._parser` (guarded by try/except); worker script sits in the installed package (writable by a model with package write access, no new privilege given exec); worker cwd honours the parent `TMPDIR` (harmless under `-I`) | T8, 13f12f3 / 5a6cde2 | Accept |
| A never-ending exec output flood keeps a CPU core busy until the timeout (memory is bounded); zombies for `yes`/`cat` when there is no init process (same as old code) | T9, b5350c8 | Accept, or add a rate limit later |
| `web_fetch` Jina path still does an unbounded `client.get` + `r.json()` (`web.py` `_fetch_jina`) | T8, 5b8b173 | Small fix (byte cap) or Q8 (default off) |
| `edit_file`/`apply_patch` measure only the new payload, so `replace_all` can grow a file past 10 MiB (documented gap) | T8, 5b8b173 | Accept or measure the result |
| Write-only files match by suffix in ANY workspace, so a user repo's `memory/history.jsonl` is denied too (matches exec's substring guard; denial text could say so) | T1, 2e93751 | Doc nit |

## 2. Test tidiness and small code nits

| Item | Found | Suggested owner / phase |
|---|---|---|
| `nanobot/agent/tools/execution.py:262-266` duplicate `_classify_violation` (used only by tests) calls `repeated_workspace_violation_error` without `raw_text`; the duplicate marker list is dead at runtime | T1 2e93751, T3 7eb5108 | Delete the duplicate or pass `raw_text` through (drift risk) |
| The exec-floor class requires BOTH the floor phrase and the denyguard marker; a future floor message that drops the marker would fall out of both classes | T3, 7eb5108 | Fold into P2 denial markers |
| `exec_session` input-guard escalation test covers the fork bomb only, not internal-state input | T3, 7eb5108 | Add a test |
| Check order in `check_session_input` differs slightly from exec (sudo, then URL) | T2, f623064 | Ignore or align |
| Recursive `list_dir` uses `rglob` and enumerates protected dirs before filtering (no file opened); prune with `os.walk` | T1, 2e93751 | Small fix |
| Escalation text of `repeated_workspace_violation_error` for non-floor denials still mentions `restrict_to_workspace` (floor denials fixed under ruling R1) | T1 / T3 | Closed by R1; listed for traceability |
| Banner tests: the image test calls the helper directly and `_banner()` is defined at the bottom of the test file | T6, 015e27b | Test tidy-up |
| Duplicate `loguru` import in the floor tests | T1, 6313a20 | Test tidy-up |
| Exec output capture: `(N chars truncated)` counts only captured chars while the next line counts discarded bytes (mixed units, `shell.py` ~476-479); unreachable `elif capped_note:` branch untested (~480-481); reader tasks cancelled but not awaited in the `finally` (~517-520); stderr flood test does not check the end of stderr | T9, b5350c8 | Test and code tidy-up |
| `redact_value` over containers of many big strings scales linearly (1.9 s for 6.4 MB, only when a record is emitted); image-generation redacted logs are not lazy | T5, c4f0cf3 | Accept |
| Existing tests changed by phase 0: 19 exact-equality assertions gained the banner prefix (T6), three "hard policy boundary" pins (T2), one exec-denyguard pin (T3), ~11 web-fetch fake clients (T8), 23 exec mocks converted to a fake-stream helper (T9). All were mechanical; listed so a later merge knows where to look | T2, T3, T6, T8, T9 | Merge note |

## 3. Doc nits

| Item | Found | Suggested owner / phase |
|---|---|---|
| Leftover "hard policy boundary" wording at `utils/runtime.py:209` and `security/workspace_policy.py:14` (the latter is the model-visible `WORKSPACE_BOUNDARY_NOTE`, deliberately not reworded) | T2 f623064, T3 7eb5108 | Decide with the owner; not pinned by tests |
| Docs cited `protected_paths.py:123-130` for the floor denial message (`reason()` starts near 152 now) | T1, 6313a20 | Re-check when the file changes |
| Spec section 2.3 rows describe pre-phase-0 behaviour, so their cites land on the fix rather than the old flaw | T11 | Rewrite when the findings table is next revised |
| Pre-existing, not from phase 0: 8 awork tests fail against core-slim and against the base tree. Root cause (corrected; earlier notes blamed the lazy `model_rebuild` order): awork's venv (Python 3.12) lacks `rapidfuzz`, because it was `uv sync`ed from awork's vendored moeka pin b9e0f080 (2026-07-14), which predates main commit 1c3c6826 (2026-09-15) that added `from rapidfuzz.distance import ...` to `nanobot/utils/file_edit_events.py` (reached from `agent/tools/filesystem.py`, imported by `_resolve_tool_config_refs` in `config/schema.py`) and declared `rapidfuzz>=3.14.6,<4.0.0` in pyproject. The lazy hook swallows every `ImportError` including `ModuleNotFoundError`, so `model_rebuild()` never runs and 7 tests surface `PydanticUserError: Config is not fully defined; you should define RunnerLimits`; the 8th (test_phrase_cache) is the missing module directly. With `rapidfuzz` importable all 8 pass on unmodified core-slim HEAD. Not a slimming, phase-0 or packaging regression (`rapidfuzz` is a declared main dependency) | T11 gate | Remedy: bump awork's moeka submodule, then `uv sync`. Optional hardening is an OWNER DECISION (not done): in `_ensure_tool_config_refs_resolved` and the module-level retry (`config/schema.py` ~734-748) re-raise `ModuleNotFoundError` whose `.name` does not start with `nanobot` (~8 lines, proven in a throwaway copy), so a missing dependency fails honestly |

## 4. Design decisions for the owner

| Item | Found | Suggested owner / phase |
|---|---|---|
| Session-input guard: buffer across writes (NUL stripping is done), or accept as a hint given E4 | T2 | Owner; P2 gate makes it moot for the harness |
| Strip zero-width/bidi characters from MCP descriptions (and possibly other model-visible external text) | T6 | Owner; P0.7 follow-up |
| One classifier: remove `execution.py` duplicate versus keep it in sync | T3 | Owner; next cleanup |
| Redaction name cap of 64 chars versus a linear-time unbounded name match | T5 | Owner |
| Description cap: 2000 chars plus ellipsis (2001) versus a hard 2000 | T6 | Owner |
| Grep: accept the deferred limits, or bound total repeat cost, memory per batch and line count | T8, ruling R3 | Owner; only if grep meets untrusted input |
| `webui_allow_local_service_access` knob: delete with the websocket gate and replace by a `net.fetch:loopback` rule (spec Q7) | spec Q7 | P2 |
| Q5: workspace == state dir (flat layout, moeka deviation): keep for the live service, host supplies `Paths` | spec Q5 | Owner; P1 |
| Q6: `restrict_to_workspace` defaults False and destructive commands allowed: keep for the bot, restricted for harness principals | spec Q6 | Owner; P1/P2 |
| Q8: Jina reader on by default (forwards URLs to a third party): default off in the core's fetch section | spec Q8 | Owner; P4 |
| Host fact: this machine's account has passwordless sudo (`sudo id` printed `uid=0`); the harness must not run under it | T2 review | Owner; harness plan |
