# Architecture review, moeka part: the consolidation and the cutover (2026-10-01)

Hostile review of `specs/003-main-consolidation` and of the candidate branch `consolidate/new-main` (tip
`bc86a31f`, read-only worktree `moeka-newmain`). The cross-project half (coupling, product assumptions,
process risks) is in the awork-resume repo, `docs/reviews/2026-10-01-architecture-review.md`; owner decisions
there and here share one id space (D-M* here, D-S* there).

Everything below that says "measured" was run on 2026-10-01 against copies: a throwaway `HOME`, a copy of the
live `config.json`, a local stub model endpoint, no network, no paid model, nothing written to the live
checkout, the service, `~/.nanobot` or any branch other than this one. The probes are in
`docs/reviews/2026-10-01-parity/` and can be re-run by anyone. This file states operational facts about one
host; if this branch is ever pushed to the public `origin`, review this file first.

## 1. Verdict

The consolidation is sound as geometry and the candidate is better prepared than the spec assumed (the
other agent already wrote `docs/consolidation-record.md` and `specs/003-main-consolidation/CUTOVER.md`, and
reports 9607 passing tests). I could not make the live bot's visible surface break in any offline probe.
But the cutover as written has gaps that matter more than the code diff does:

1. There is no usable backup of the live state. The RAID1 backup tier is offline and the backup timer is
   disabled. The cutover's own backup step copies only the usage database and config, not the session
   database or the memory files.
2. The runbook's `uv sync --locked` step is unnecessary for this cutover and is the one step with a known
   production incident.
3. The "selective restore is lower risk" claim is true only for the gateway packages. The live bot takes
   +21.5k lines of kernel changes across 91 modified shared runtime files. The risk did not go away; it moved
   from conflicts to behaviour drift, which is measurable and was measured here (section 3).
4. The live bot does not run on the kernel's guarantees after the cutover. It runs through the legacy adapter
   with the I2 overlap allowance. The spec should say so, so nobody reads "consolidated" as "I1-I6 apply to the
   bot".

Recommendation: GO for the cutover only after the stricter checklist in section 5 passes. NO-GO on pushing
`main` to the public `origin` until a 48 hour soak, and keep the push separate from the cutover.

## 2. What the live state is (verified 2026-10-01)

| Item | Fact | Consequence |
|---|---|---|
| Service | `moeka.service` active since 2026-09-30 01:16, `NRestarts=0`; its unit file is `disabled` (not enabled at boot); user linger is on | A reboot leaves the bot down until someone starts it. A rollback that involves a reboot is a hidden outage |
| Process | `python -m nanobot gateway --foreground`, cwd the live checkout, venv python 3.12 | The unit, launcher and venv do not change in the plan: good |
| Code | live checkout on `main` `54069bb2`, one untracked directory (`docs/core-map/`, two files, older drafts) | Blocks `git merge --ff-only` (CUTOVER already says so) |
| Config | `config.json` validates on `main` and on `consolidate/new-main`; `core-slim` REJECTS it (`channels`, `api`, `gateway` are extra-forbidden there) | Confirms the base-choice reasoning: `core-slim` could never have run the live config |
| Secrets | every token and key in `config.json` is a `${VAR}` reference; values are in the checkout's gitignored `keys.env` | No secret in a tracked file. Good |
| Sessions | one SQLite database in the ADR-0001 location (migrated 2026-09-30 01:16); no database at the legacy location; the old per-channel JSONL files are already renamed `.imported` | The session migration is already done and is NOT part of this cutover |
| Cron | `cron/jobs.json` v1, two system jobs (`dream` every 2 h, `heartbeat` every 30 min) | `nanobot/cron`, `gateway`, `triggers`, `channels`, `bus` are byte-identical between `main` and the candidate |
| Usage DB | `llm_usage.sqlite3`, two rows, user_version 1 | New code migrates it to version 2 (see R7) |
| HTTP API server | configured (`api.port` 8900) but not listening | The restored `nanobot/api/server.py` is not exercised by the live bot today |
| Backups | `/mnt/jesse` is not mounted; `ops/jesse.offline` marker exists (2026-09-30 17:46); the weekly workspace backup aborts when the mount is missing; its timer is `disabled` | No off-disk copy of the sessions, memory or config exists right now |
| Tools on the host | the `sqlite3` command-line tool is not installed | CUTOVER's verification commands (`sqlite3 ...`) fail as written; use Python's `sqlite3` or `ops/sqlite-backup.py` |

## 3. Measured parity, old `main` versus candidate

| Probe | Result |
|---|---|
| Live config validates | both yes; `core-slim` no |
| Channel plugin list (`nanobot plugins list`) | identical (telegram and discord enabled) |
| Gateway toolset (`AgentLoop.from_config(... host_tools=True ...)` with the live config) | identical except one NEW tool, `defer_action` |
| Exec child environment keys (`allowedEnvKeys`: canvas, openrouter, anthropic, openai, docker) | identical |
| Provider request to a stub OpenAI-compatible endpoint (same config, `openrouter` pointed at the stub) | headers identical; body identical except the tool list (`defer_action`) and the system prompt |
| System prompt | candidate is 213 bytes shorter; the changed lines replace advice to use the `bg_shell` action ("you will be woken automatically") with advice to use `exec` with `yield_time_ms` and `exec_session`, and say "nothing wakes you when a session finishes". `bg_shell` is not a registered tool on either side, so the old wording was already wrong; the new wording is a visible behaviour change in how the bot talks about long jobs |
| Session written by old code, read by new code, then new code's write read by old code | both directions work (history intact, roles intact) |
| Usage DB: old, then new, then old again on a copy | works; new adds two nullable columns and `user_version` 2; old code resets `user_version` to 1 and keeps working |
| Import walk of every `nanobot.*` module on the candidate | only third-party gaps in the slim test venv (`aiohttp`, `websockets`, `telegram`, `slack_sdk`), none inside the project |

Not covered (so not claimed): a real Telegram or Discord turn, streaming edits, the WebUI bundle, an
actual cron fire, a Dream run, MCP servers, vector memory (config enables it; the `vec` plugin is not
enabled on either side), canvas (enabled in config but no such channel exists on either side: a dormant
pre-existing mismatch, not a regression).

## 4. Risk register (ranked)

Likelihood and impact are my judgement (L/M/H). "Evidence" is something I ran or read.

| # | Risk | L | I | Evidence | Mitigation |
|---|---|---|---|---|---|
| R1 | No verified backup of live state before cutover; CUTOVER backs up usage DB and config only | H | H | RAID mount missing, marker file, backup timer disabled; CUTOVER precondition 3 | Gate G3: back up the session DB triplet with the SQLite backup API, memory files, cron, config, to two physical disks; verify by opening and counting; do it with the service stopped |
| R2 | `uv sync --locked` in the runbook strips channel runtime deps and made the 2026-09-28 deploy crash-loop | M | H | `.agent/deploy-runbook.md` step 5b; `uv.lock` is byte-identical to `main`'s; the venv's editable `.pth` already points at the repo root, so `import moeka` works after a plain checkout | Skip the step. If the owner still wants it, the `plugins enable` re-run and import check are mandatory and part of the go criteria |
| R3 | Shared-runtime drift: +21.5k/-1.4k lines in `nanobot/` (91 modified, 77 added files) land in the live bot at once | M | M | `git diff --shortstat main HEAD -- nanobot` | Section 3 probes pass; keep them as a repeatable script; add the canary (G4) for what the probes cannot reach |
| R4 | `host_tools` defaults to false in `AgentLoop`; forgetting it silently drops `message`, `cron`, `run_cli_app`. Three gateway call sites pass it today | L | H | `nanobot/agent/loop.py`; cron coordinator asserts, the `message` tool does not | Add a gateway test that asserts the exact tool set; log a warning when a loop with a `bus` has no `message` tool |
| R5 | Rollback is weaker than it reads: the backup claim is empty (R1), the venv is not rolled back by `git reset`, and a rollback after a public push needs a force push | M | H | CUTOVER "Rollback" | Do not run `uv sync`; take the backup; do not push before the soak; rehearse the rollback (G6) |
| R6 | One-way data changes are small but real: usage DB v2; history entries gain a `gen` stamp; session metadata gains `_history_gen`; a rewind now deletes the stale archive entries from `history.jsonl` | L | L | diff of `sqlite_store`, `llm_usage/store`, `memory`, `session/manager`; round trips in section 3 | Old code tolerates all of it; the only irreversible effect is rewind-time deletion in `history.jsonl`, covered by the backup |
| R7 | Service not enabled at boot, backup timer disabled | M | M | `systemctl --user is-enabled` | Owner decision D-M4 |
| R8 | CI cannot protect `main`: floor 5400 tests versus about 9600; strict `basedpyright` reports 791 errors (289 on old `main`) | H | M | CUTOVER owner decision 2 | D-M5 |
| R9 | Publishing: `origin` is PUBLIC. Pushing `main` publishes 187 commits and `.agent/` reports containing host paths; rollback after that needs a force push | M | M | `gh repo view` says PUBLIC; `git grep /home/muk` finds 41 lines in tracked files | Separate cutover from publish; review `.agent/` for host detail first |
| R10 | Local-only work: `core-slim` is 11 commits ahead of origin; `consolidate/new-main` 27; `feat/usage-surface` 19; `rsi-harness-spec` 5; `feat/setup-harness` 1. awork-resume vendors schemas from `feat/usage-surface` commit `31dc71a0`, which exists only here | M | H | `git rev-list --count <branch> --not --remotes` | Section 6 of the awork-resume review: bundles to two disks and push plan |
| R11 | The gateway still runs the legacy `AgentLoop.from_config` path with the legacy adapter: paths overlap is allowed (a warning is logged), so I2 does not hold for the bot, and I1 holds only because the adapter is on the allow-list | H | L | log line "overlap (legacy flat layout); allowed for now" in the probe | State it in spec 003 (done on this branch); stage 2 stays an open owner decision |
| R12 | Untracked `docs/core-map/` in the live checkout blocks the fast-forward and holds the only copy of two older drafts | H | L | `diff -rq` against the tracked directory | Move aside (CUTOVER step), do not delete |
| R13 | Earlier test runs polluted the live sessions directory (24 stray test databases dated 2026-09-17, 2.5 MB) | L | L | `~/.nanobot/sessions/*/.workspace` point into `/tmp/pytest-of-muk` | Harmless; tests now have `_home_guard`; keep running tests only via `scripts/test-docker.sh` |
| R14 | Consumers' dependency weight: base dependencies grew back from 168 to 188 locked packages and a build hook runs the WebUI build on non-editable builds | M | L | `uv.lock` package counts; CUTOVER "Consumers" | awork pins an editable path or sets `NANOBOT_SKIP_WEBUI_BUILD=1`; awork-resume is unaffected (independent, 30 packages) |

## 5. Selective restore versus a separate gateway repository

Selective restore (what was done) wins for now, for a narrower reason than the spec gives.

- What it keeps constant: unit file, launcher, venv, config schema, channel discovery (entry points inside
  `nanobot/channels`), cron and triggers (byte-identical), HTTP server, data locations. Every item on the
  question's list is unchanged except the shared runtime.
- A gateway-as-plugin repository would change all of them at the same moment: the gateway reaches the agent
  through the legacy loop (FR-009, stage 2 not done), `Config` carries the channel sections, the loop holds
  cron and trigger coordinators through the `host_tools` seam, and the WebUI ships in the same wheel.
  Splitting it first would need a stable public gateway seam that does not exist.
- What it does NOT reduce: the 164 kernel commits reach the live bot either way. Selective restore only
  removes the second source of risk (a bad merge of the gateway).
- The cheaper structural fix for the real hazard, "the service runs whatever is checked out" (contract R6),
  is not a repository split but a pinned deploy checkout (D-M1).

The plugin-repo option becomes reasonable after stage 2, when the gateway uses `moeka.Kernel` and a channel
SDK is tested by a second host.

## 6. Where rollback is weak

1. Data: nothing that the new code writes breaks the old code (round trips in section 3), so the data side is
   safe to roll back. It is only unrecoverable where the new code deletes: a rewind removes archived entries.
   That is why the backup must exist.
2. The venv: `uv sync` is not undone by `git reset`. Skip it (R2).
3. The refs: the rollback tag is a local ref; also write the sha (`54069bb2`) into the operator's notes.
4. The public ref: a rollback after pushing needs a force push on a public branch. Do not push before the soak.
5. The unit file is `disabled`: a rollback that includes a reboot leaves the bot off.

## 7. Stricter go/no-go checklist for the cutover

Every gate has a pass criterion. A missing result is a NO-GO, not a skip. Gates G0-G3 and G6 run before
anything changes; G4 is the canary; G5 is the cutover; G7 is the soak.

| Gate | Check | Pass |
|---|---|---|
| G0 | `git -C ~/projects/moeka rev-parse HEAD` is `54069bb2`, branch `main`, status shows only `docs/core-map/`; no other agent or shell is using that checkout | exact match, recorded with a timestamp |
| G1 | the tip to cut equals the tip that was tested: `git rev-parse consolidate/new-main` equals the sha in the "Verified state" block, or the suite is re-run (`scripts/test-docker.sh`) on the new tip. `main` is an ancestor of it and so is `core-slim`; `git diff --stat main consolidate/new-main -- nanobot/{cron,gateway,triggers,channels,bus} bin` is empty | all true; 0 failed |
| G2 | parity probes (`docs/reviews/2026-10-01-parity/`) re-run on the exact tip: same tool set (plus `defer_action` only), same exec env keys, same plugin list, live config validates, request body diff limited to tools and prompt | no other difference |
| G3 | with the service STOPPED: SQLite backup (not `cp`) of the session database to two physical disks (root volume and `/mnt/arteta`), plus `memory/`, `cron/jobs.json`, `config.json`, `llm_usage.sqlite3*`, `SOUL.md USER.md AGENTS.md TOOLS.md HEARTBEAT.md`; open each copy, run `PRAGMA integrity_check`, compare `count(*)` of sessions and messages to the live file; write a checksum list | counts equal, integrity ok, two disks |
| G4 | canary: a throwaway Telegram bot token (free from BotFather) with the owner's id in `allowFrom`, a COPY of the workspace (`cp -a`, different `MOEKA_WORKSPACE`, port and `--config`), the candidate code in its own worktree; send three real turns including one that needs history, one tool call and `/status`; wait for one heartbeat | replies arrive within 60 s, history used, no ERROR lines, heartbeat fires |
| G5 | cutover per CUTOVER.md steps 1-3 and 5-8 with step 4 (`uv sync`) removed; start; watch `journalctl --user -u moeka.service -f` for 10 minutes | "Registered N tools" contains `message`, `cron`, `run_cli_app`; "Channels enabled" lists telegram and discord; `/health` says ready; no traceback |
| G6 | rollback rehearsed BEFORE G5 in a scratch clone: `reset --hard pre-consolidation-main`, start the old tree against a COPY of state, answer one turn | works in under 5 minutes, written down |
| G7 | soak 48 hours: a real history turn, a cron fire, a Dream run, one `/new` and one rewind, usage row count grows; then decide on the public push | no regression reported; owner approves the push |

Automatic NO-GO triggers: any unverified backup; any tool-set difference beyond `defer_action`; the service
fails to reach ready within 60 s of start; a reply without prior context on a chat that has history.

Corrections to CUTOVER.md that follow from this review: add the session database and memory to the backup
list; use Python in place of the missing `sqlite3` tool; drop step 4; separate step 8 (push) from the cutover
and gate it on G7.

## 8. Owner decisions (moeka)

| Id | Decision | Options | Recommendation |
|---|---|---|---|
| D-M1 | Where does the live service run from after this cutover? | A: keep the live checkout on `main`; B: a dedicated deploy worktree on a `live` branch that moves only when the owner moves it | B, as a follow-up after this cutover. It removes contract R6 (the service following whatever is checked out). One unit-file edit |
| D-M2 | Keep `uv sync --locked` in the cutover? | keep / drop | Drop (R2) |
| D-M3 | Require a canary on a throwaway bot token and a 48 hour soak before the public push? | yes / no | Yes. The canary is the only real-channel test available |
| D-M4 | Enable `moeka.service` at boot and fix the backup tier before the cutover? | enable now / defer | Enable the service; and do not cut over until a backup target is verified (the session database is the one irreplaceable file). Remounting the RAID or using `/mnt/arteta` both satisfy G3 |
| D-M5 | CI: raise the test floor and scope the strict type check? | raise floor to about 9000 and scope pyright to the kernel packages / leave | Raise and scope. A gate that cannot fail is not a gate |
| D-M6 | Should the review and any operational detail go to the public repo? | keep private / push | Keep this file local or move operational facts to the private awork-resume repo before any push |

## 9. Changes made on this branch (review/arch)

- this file and the parity probes under `docs/reviews/2026-10-01-parity/`;
- `specs/003-main-consolidation/`: status note, FR-012 to FR-016, SC-007 to SC-009, tasks T019 to T025;
- `.specify/memory/constitution.md` 1.1.0: pins must be resolvable from `origin`, cutover gates;
- `specs/CLARIFY-LOG.md`: Q14 to Q16 pointing at D-M1 to D-M5.

No code, no other branch, no live path was changed.
