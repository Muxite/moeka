# Operating Moeka: jobs, access and safe self-change

This page covers how Moeka works as a trustworthy operator for one person's machines and projects. It
explains what you can see, what it may touch, and how you recover when a change goes wrong. The design
record is [`specs/015-central-plan/spec.md`](../specs/015-central-plan/spec.md).

```mermaid
flowchart LR
    Owner[You in chat] --> Kernel[Moeka kernel]
    Kernel --> Job[(Job record)]
    Kernel --> Grants{Access grants}
    Grants --> Tools[File tools and shell]
    Tools --> Wonhee[wonhee]
    Wonhee --> Job
    Job --> Owner
    Owner -->|/stable /revert| Code[(Running code)]
```

## Job records

- Every message you send that is a real request opens one **job**. Dream, heartbeat and cron turns do not.
- A job moves through these states: `heard`, `needs_answer`, `working`, `blocked`, `done`, `failed`,
  `outcome_unknown`.
- Each state change is an event with a real timestamp. A `failed`, `blocked` or `outcome_unknown` event
  always names the **side** that stopped it (`moeka`, `wrapper`, `wonhee`, `stagecraft`, `telegram`,
  `policy`) and a **cause** code.
- A reply to a question Moeka asked resumes the waiting job instead of opening a new one.
- When Moeka runs wonhee work, the creative bridge records each wonhee submission and its delivery as a job.
  A job becomes `done` only when the file is sent, never merely because it left wonhee's active list.
- Records live in `jobs.sqlite3` next to `sessions.db` (hidden from the agent by the sessions protection).

Read them from chat:

- `/jobs` lists recent jobs with state and, for a failure, `(side: cause)`.
- `/jobs <id>` shows one job's history, timestamps and the detail of each step.
- `/wonhee status` includes the tracked wonhee jobs.

Typical causes you will see:

- `policy: workspace_violation` means a file access was outside every grant.
- `wrapper: timeout` means the link to wonhee timed out; wonhee itself may be fine.
- `wonhee: stalled` or `submission_failed` means wonhee reported the problem.
- `telegram: <ErrorName>` means a finished file could not be sent.
- `moeka: empty_final_response` means the turn ended after tools ran without a reply; the detail lists
  what ran and the last error.

## Access grants

Three config keys under `tools.file` control what the agent may touch outside its workspace. They need
`tools.restrictToWorkspace: true`.

- `readRoots`: directories it may read.
- `writeRoots`: directories it may write, and read. **A read root never allows writes.**
- `protectSource` (default `true`): refuse file-tool writes into the tree Moeka runs from.

```json
{
  "tools": {
    "restrictToWorkspace": true,
    "file": {
      "readRoots": ["/home/you/projects", "/mnt/shared"],
      "writeRoots": ["/home/you/projects/wonhee"]
    }
  }
}
```

The same roots feed the `bwrap` shell sandbox (`tools.exec.sandbox: "bwrap"`): read roots become
read-only binds and write roots read-write binds, so a read root is read-only in the shell too. Without the
sandbox the shell is only guarded by path checks, and a read root is not enforced against it.

A denial names the missing grant, for example `no write grant covers /x; only the owner can add one with
tools.file.writeRoots`. Only you edit this config. The agent cannot grant itself access.

### What stays blocked inside a granted root

- `keys.env`, every `.env` and `.env.*` file (templates `.env.example`, `.sample`, `.template`, `.dist`
  stay readable), moeka's sessions, auth, peers, config, locks and usage database.
- Writes into the running source tree. In the shell sandbox that tree is re-bound read-only.
- In the shell sandbox, `.env` files are replaced by an empty file. The scan is bounded (four levels deep,
  generated directories such as `node_modules` skipped, refreshed every 30 seconds). A `.env` created after
  the scan or deeper than that is not masked in the shell. The file tools check names at access time.

## Stable versions and revert

Moeka cannot edit its own code through its tools, so changing the running version is your decision.

```mermaid
stateDiagram-v2
    [*] --> Running
    Running --> Marked: /stable
    Marked --> Running: new commits
    Running --> Reverted: /revert latest
    Reverted --> Running: /revert back
    Running --> Reverted: boot guard after 3 unhealthy boots
```

- `/stable [note]` tags the running commit `moeka-stable/<UTC stamp>`. It refuses a dirty tree, so a mark is
  always reproducible.
- `/revert` lists the marks. `/revert latest`, `/revert previous` or `/revert <mark>` checks that version out
  (detached HEAD), then restarts the process in place. It is back in a few seconds and reports
  `Reverted to <mark> (<sha>)`.
- A dirty working tree blocks the revert until you add `force`, which stashes your changes as
  `moeka-revert-<stamp>`.
- `/revert back` returns to the branch you came from. Chained reverts still remember the original branch.
- Only stable marks are valid targets, never an arbitrary commit.
- Both are slash commands handled before the model sees your message. The agent has no tool for them, and
  its file tools and shell cannot write the repository or the revert state.

### Boot guard

- `bin/moeka.sh run` calls `scripts/selfrevert.py guard` before starting the gateway. It uses only the Python
  standard library, so it works even when the app code is broken.
- Each start of the same commit counts as unhealthy until the gateway has stayed up 60 seconds. Three
  unhealthy starts within ten minutes, with a stable mark to return to, trigger an automatic revert to the
  newest mark. With no mark, it logs and does nothing.
- It runs for the default instance only, because a revert moves the shared checkout. It never blocks startup.
- State is in `<workspace>/.boot-guard.json` and `<workspace>/revert-state.json`. The file tools cannot
  write either.
- A pip install has no git checkout, so `/stable` and `/revert` say so and the guard does nothing.

## Hearing you, telling you, and not hiding a dead turn

- **Acknowledgement.** If a request is still running after 8 seconds, moeka sends "Heard. Working on it
  (job id)". Quick answers get no ack, so it is not noise.
- **Status.** The `job_status` tool reads the record, so a status answer is never recalled from earlier
  messages. `/jobs` shows the same data.
- **Dead turns.** If a turn ends with no answer, moeka replaces the generic "couldn't produce a final answer"
  with a report built from the record: the state, which side stopped it, the cause and the timings. An
  unknown outcome says it is unknown.
- **Proactive messages.** A job that needs your answer, is blocked, finishes or fails is sent to you on its own,
  on the chat or topic it came from. It is a durable outbox: a message that may have gone out when the
  process died is marked uncertain and never sent twice. A chat turn is not re-announced (you already got
  the answer), wonhee jobs are reported by the bridge after the real delivery, and a request orphaned by a
  restart is reported as outcome unknown.
- **Children.** A wonhee job or worker started from a chat turn is linked to it, and `/jobs <id>` lists the
  children.

```mermaid
flowchart LR
    Req[Your request] --> Job[(Job record)]
    Job -->|still running after 8s| Ack[Heard, job id]
    Job -->|done, failed, blocked, needs answer| Outbox[(Notification outbox)]
    Outbox --> Notifier[Notifier]
    Notifier --> You[Same chat or topic]
    Job -->|turn ended with no answer| Report[System-built report]
    Report --> You
```

## Delegating to Claude Code and Codex

Moeka keeps your intent and briefs a stronger agent. The worker tools start, resume, inspect and stop
sessions by explicit job id. They load only when a backend is enabled.

- **Two accounts, never swapped.** `worker_start` takes `backend` (`claude` or `codex`). Each has its own
  usage, limits and login directory. A disabled, missing or out-of-usage backend fails the job with a named
  cause (for example `claude:usage_limit`). Moeka never falls back to the other one.
- **Faithful briefing.** The request goes to the worker verbatim with your constraints. The worker is told not
  to guess requirements, to stop and ask when a decision is yours, and to end with a status line
  (`completed`, `needs_user`, `blocked`).
- **Follow-up resumes the same session.** `worker_follow_up` resumes the stored session id. Moeka passes your
  answer to a worker that asked a question. It never uses "latest session".
- **Mechanical watching.** Code, not a model, watches the process, the output log, the exit status and a
  timeout. You are messaged when a worker finishes, asks, is blocked, fails or times out.
- **Reports are claims.** A result is stored as "Claude reports…" or "Codex reports…". Merging and deploying
  stay yours.
- **Containment.** Workers run detached, so a gateway restart does not kill them, and are re-attached on start
  (or marked unknown if they vanished). They run under bwrap: only the project directory is writable, `.env`
  files are masked, moeka's source is read-only, and other projects, ssh keys and moeka's state are not
  mounted. Claude Code runs without your hooks, plugins or MCP servers. One writer per project at a time.
  The project must lie inside `tools.file.writeRoots` and outside moeka's own files.

```mermaid
sequenceDiagram
    participant You
    participant Moeka
    participant Manager as Worker manager
    participant Worker as Claude Code or Codex
    You->>Moeka: fix the failing export
    Moeka->>Manager: worker_start(backend, project, request)
    Manager->>Worker: detached, sandboxed run
    Moeka-->>You: Heard. Working on it (job id)
    Worker-->>Manager: question
    Manager-->>You: needs your answer
    You->>Moeka: the users table
    Moeka->>Manager: worker_follow_up(job id, answer)
    Manager->>Worker: resume the same session
    Worker-->>Manager: result and status line
    Manager-->>You: Claude reports: done, checks, what remains
```

## Quiet-time review and the evidence rule

- **Review.** When no job is active, a reviewer looks at the last two weeks of the record. It looks for the same
  kind of request completed again and again, and for one cause stopping several jobs. It asks you once per
  pattern, inside quiet hours and at most one message a day. It builds nothing by itself.
- **Evidence rule, in code.** Dream may stage a new skill only when the record shows the same request completed
  at least 3 times (each pattern pays for one skill), or you approved one with `/approve-skill [note]`. Without
  evidence the write is refused with the reason, and the agent is told to put the lesson in memory or improve an
  existing skill. The model cannot approve its own skill: `/approve-skill` is an owner command with no tool.
- **Still reversible.** New skills land in `skills-pending/` until you accept them.

## Limits worth knowing

- The job record shows what moeka and its wrappers observed. Linking a wonhee job to the chat turn that
  started it is a heuristic (the newest chat job of the same chat in the last hour), not a signed reference.
- A wonhee job that leaves its active list without a failure or a file stays `working` with a note that
  says so. It is never reported as done on a guess.
- Grants are global config, not per role.
- Real containment against a determined agent is the OS boundary (the sandbox, a separate user, a
  container). These checks make mistakes visible and stop the common ones.
