# Deploy-blockers report — 2026-09-18

Response to the whole-branch review's four findings (post-batch-7,
post-post-sync-fixes). Worktree `/home/muk/projects/moeka-sync`, branch
`merge/upstream-main-2026-08-05`. Nothing pushed. `/home/muk/projects/moeka`
confirmed on `main`, untouched. No production code (`nanobot/` runtime
logic) changed by any of the four items — test files, a deploy doc, and
the test-runner script only.

## 1. Deploy-blocking: rollback section rewritten

Confirmed both claims were false, by reading `migrate_session_database`
(`nanobot/session/sqlite_store.py`) directly: it copies to the
destination, verifies, then unlinks the **source** — always, in both
directions. A "rollback" is the same tool run in reverse, so it deletes
the ADR-0001 copy once the workspace copy is verified; there was never a
free "verified copy to compare against" unless one was taken
independently. Also confirmed the second claim: this branch's code only
ever resolves sessions via `default_sessions_root()`
(`nanobot/session/sqlite_store.py`) / `build_default_session_manager()`
(`nanobot/agent/loop.py`) — never reads `<workspace>/sessions.db`
directly — so moving the file back without also reverting
`/home/muk/projects/moeka`'s checkout leaves the service looking at an
empty ADR-0001 location a second time.

`.agent/deploy-runbook.md` rewritten:
- New **step 3, "optional but recommended: take an independent safety
  copy"** — a plain `cp -a` before the real migration, explicitly
  independent of the migration tool's own lifecycle, exactly as
  instructed ("if a safer rollback wants a copy taken before migrating,
  say so explicitly as a step").
- Step 4 ("perform the migration") now states plainly, in bold, that
  success means the source is deleted — not implied, not buried in a
  cross-reference.
- The rollback section leads with an explicit two-point correction (both
  false claims named directly) before any commands, then a 4-step
  sequence that (a) uses the migration tool to move the data back, naming
  that this deletes the ADR-0001 copy unless a step-3 backup was taken,
  and (b) makes reverting `/home/muk/projects/moeka`'s checkout back to
  `main` an explicit, separate, required step — with a
  `git -C /home/muk/projects/moeka branch --show-current` sanity check
  before and after, since this runbook cannot itself specify the exact
  revert mechanism (depends on how the eventual deploy actually moves that
  checkout off `main`) but can and does insist it happens before
  restarting the service.

## 2. Deploy-blocking: wrong `nanobot` binary

Confirmed: `which nanobot` → `~/.local/bin/nanobot` (the `main` install);
`nanobot sessions --help` fails with "No such command 'sessions'".
Confirmed `main`'s CLI genuinely lacks the subcommand this runbook depends
on (it's part of the ADR-0001 conversion, not yet in `main`).

Added a new section, **"Before you start: `nanobot` on your PATH is the
`main` install, not this branch"**, before the procedure, with the exact
resolution: `./bin/moeka.sh exec sessions migrate ...` (verified this
worktree already has a `.venv` installed, and `./bin/moeka.sh exec
sessions --help` correctly lists `migrate`/`restore-workspace`). Every
command in the procedure now uses this form, not bare `nanobot`. Also
noted `exec` already supplies the leading `nanobot` itself — the earlier
draft's ambiguity about whether to write `exec -- nanobot sessions ...`
would have double-invoked it.

## 3. 8 dark SSRF tests recovered — DNS resolution baked into the test image

Confirmed the review's framing precisely: these tests need
`example.com`/`example.org` to *resolve* (`validate_url_target()`'s
`socket.getaddrinfo` check), not to actually be reached — every real HTTP
call in the affected tests goes through `_FakeHttp`, a fake in-test
client.

Tried baking a `RUN echo ... >> /etc/hosts` into `Dockerfile.test` first;
rejected it once confirmed (from how `scripts/test-docker.sh` invokes
`docker run`) that Docker regenerates each container's `/etc/hosts` at run
time regardless of what a build-time `RUN` wrote there — a build-time
hosts file edit would be silently ineffective. Used the mechanism Docker
actually supports for this instead: `--add-host` on `docker run`, added in
`scripts/test-docker.sh` (ours, not upstream's test files — nothing under
`tests/` or `nanobot/` was touched for this item):

```
--add-host example.com:203.0.113.10 --add-host example.org:203.0.113.11
```

Both addresses are TEST-NET-3 (RFC 5737, permanently reserved for
documentation, never routable) — confirmed via
`nanobot/security/network.py`'s `_BLOCKED_NETWORKS` that this range is
**not** treated as private/internal, so the SSRF guard correctly evaluates
them as ordinary public addresses, exactly like a real hostname would
resolve, without needing any actual network reachability.

`tests/_capabilities.py`'s `has_dns_egress()` probe and the `skipif`
markers on the 8 dingtalk tests and the 2 `test_exec_guard_allows_public_urls`
parametrized cases were **not removed** — kept exactly as instructed, so
they remain correct for any environment that genuinely lacks resolution
(a contributor's local machine without these hosts entries, some other CI
image). With `--add-host` now supplying real resolution inside this Docker
image, `has_dns_egress()` evaluates true there and all 10 tests run for
real. Verified directly (not just via the aggregate count):

```
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_rejects_private_redirect_result PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_does_not_follow_remote_redirects_by_default PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_follows_safe_redirect_when_explicitly_enabled PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_blocks_cross_host_redirect_without_allowlist PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_allows_cross_host_redirect_when_allowlisted PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_read_media_bytes_blocks_private_redirect_even_when_redirects_enabled PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_send_media_ref_short_circuits_on_download_transport_error PASSED
nanobot/channels/dingtalk/tests/test_dingtalk_channel.py::test_send_media_ref_short_circuits_on_upload_transport_error PASSED
tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls[wget ... example.com] PASSED
tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls[python3 ... example.com] PASSED
```

**No failures among the 8 — no genuine finding to report.** Per
instructions, if any had failed this would have been reported instead of
re-skipped; none did.

## 4. 2 npx tests made unconditional

Confirmed the review's diagnosis by reading `mcp_presets_test_action()`
directly: both tests mock `connect_mcp_servers`, but
`_command_available(cfg.command)` (`cfg.command == "npx"` for the
playwright preset) is checked *before* that mock is ever reached — no real
`npx` process is spawned by either test, mocked or not. The sibling test
`test_test_mcp_preset_reports_missing_dependency` already demonstrated the
exact pattern needed: `monkeypatch.setattr("nanobot.webui.mcp_presets_api.shutil.which", lambda _command: None)`.

Applied the mirror-image patch (`lambda _command: "/usr/bin/npx"`) to both
`test_test_mcp_preset_connects_and_reports_tools` and
`test_test_mcp_preset_inspects_tools_outside_the_enabled_allowlist`, and
removed their `@pytest.mark.skipif(not has_npx(), ...)` markers along with
the now-unused `has_npx`/`NPX_SKIP_REASON` import. Both run unconditionally
now, everywhere, regardless of whether the environment actually has `npx`.

## Cheap cleanups

- Deleted the vestigial `retain_recent_legal_suffix` stub method on
  `_FakeSession` in `tests/cli/test_commands.py`
  (`test_heartbeat_empty_response_is_not_evaluated`, formerly line 2118).
  Confirmed via repo-wide grep it was referenced nowhere else — the method
  doesn't exist on the real `Session` class, was never asserted on by this
  test, and existed only as dead test fixture surface that could have
  masked a future contract change going unnoticed.
- Removed `"nanobot.heartbeat"` from `tests/core/test_import_boundary.py`'s
  `_FORBIDDEN` tuple. Confirmed `nanobot/heartbeat/` does not exist
  anywhere in the tree (`ls` fails) — the review traced its removal to
  upstream `d81aa5a4`, a legitimate upstream refactor already merged in
  batch 5.

## Docker numbers (authoritative, `scripts/test-docker.sh`, full suite)

```
7971 passed, 0 failed, 56 skipped, ~267s
```

All 56 remaining skips are legitimately platform-specific (macOS
Seatbelt, Windows named pipes/ACLs/PowerShell) or missing genuinely-absent
optional extras (`sqlite_vec`, `lark-oapi`/Feishu, no `OPENROUTER_API_KEY`
in `keys.env` for the live-provider test) — none are DNS- or
npx-dependent anymore. Up from the post-sync-fixes baseline of
`7959 passed, 0 failed, 68 skipped`: net +12 passed (the 8 dingtalk +
2 exec-guard params that now run for real, instead of skip, plus the 2
npx tests that now always run instead of conditionally skipping), -12
skipped, matching exactly.

## Verification

```
git diff 2fb16593988b9e85131e02f395bb9a5108e220e7 -- nanobot/session/manager.py
```
→ empty (untouched — none of these four items touch it).

```
git -C /home/muk/projects/moeka branch --show-current
```
→ `main`, confirmed before and after. `~/.nanobot-sessions` was only read
(`ls -ld`, to confirm the permission gap the runbook now documents) — not
modified, per the constraint to leave it strictly read-only.

`ruff check` clean on every touched file.

## Files touched

- `.agent/deploy-runbook.md` — rewritten: correct rollback semantics, the
  `bin/moeka.sh exec` invocation fix, a new independent-backup step, and a
  `chmod 700` step for the sessions workspace-id subdirectory (documented
  only — not executed by this session).
- `scripts/test-docker.sh` — `--add-host` for `example.com`/`example.org`
  against TEST-NET-3 addresses, so DNS-dependent SSRF-guard tests get real
  resolution inside the authoritative Docker run.
- `tests/webui/test_mcp_presets_api.py` — 2 tests now mock
  `shutil.which` instead of skipping on missing `npx`.
- `tests/cli/test_commands.py` — dead `retain_recent_legal_suffix` stub
  removed.
- `tests/core/test_import_boundary.py` — `nanobot.heartbeat` dropped from
  the forbidden-import list (package no longer exists).
