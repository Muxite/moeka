# Memory in nanobot

nanobot's memory is built on a simple belief: memory should feel alive, but it should not feel chaotic.

Good memory is not a pile of notes. It is a quiet system of attention. It notices what is worth keeping, lets go of what no longer needs the spotlight, and turns lived experience into something calm, durable, and useful.

That is the shape of memory in nanobot.

## The Design

nanobot does not treat memory as one giant file.

It separates memory into layers, because different kinds of remembering deserve different tools:

- `session.messages` holds the living short-term conversation, persisted to `sessions.db` (see [Session storage](#session-storage-sessionsdb) below).
- `memory/history.jsonl` is the running archive of compressed past turns.
- `SOUL.md`, `USER.md`, and `memory/MEMORY.md` are the durable knowledge files.
- `memory/vec.db` is the optional hybrid FTS5 + vector store — semantic recall over `MEMORY.md`, `history.jsonl`, skills, and any host-ingested documents (see [Semantic & hybrid retrieval](#semantic--hybrid-retrieval-vecdb) below).
- `GitStore` records how those durable files change over time.

This keeps the system light in the moment, but reflective over time.

## Session storage (`sessions.db`)

Short-term conversation state no longer lives in per-session `.jsonl` files.
As of the 2026-06 overhaul, `nanobot/session/manager.py` persists every
session to a single SQLite database, `<workspace>/sessions.db`, opened in
**WAL mode**. SQLite's own locking replaces the old bespoke cross-process
`FileLock` — safe even if a stray second moeka process starts alongside the
systemd-managed one.

- On first startup against an existing workspace, any legacy `sessions/*.jsonl`
  files are imported once ("newer wins" on conflicting keys) and renamed to
  `*.jsonl.imported` so the import doesn't repeat.
- `SessionManager.dump_jsonl(session_key)` still exports a session back to the
  old flat-file format for debugging or manual inspection.
- This only affects `session.messages` (the live conversation window); the
  Consolidator/Dream layers below (`history.jsonl`, `MEMORY.md`, `SOUL.md`,
  `USER.md`) are unaffected and still plain files under `memory/`.

## Semantic & hybrid retrieval (`vec.db`)

Alongside the file-based durable memory above, moeka can also index content
into a SQLite-backed semantic store at `<workspace>/memory/vec.db`
(`nanobot/core/vec_store.py`, enabled via `agents.defaults.vec.enable`). It
holds four logical stores:

- `memory_chunks` — `MEMORY.md` split by section headers
- `history_entries` — `history.jsonl` entries
- `skills` — skill definitions (indexed at startup, used to keep large skill
  lists within context)
- `documents` — host-supplied text ingested via `MoekaCore.ingest()` /
  `ingest_text()` (see [`docs/python-sdk.md`](python-sdk.md)), optionally split
  into named collections

Each document chunk carries metadata (`source`, `tags`, `created_at`) and
supports three retrieval modes:

- **`vec`** — semantic k-nearest-neighbor search over sentence-transformer
  embeddings (needs the `moeka[vec]` extra)
- **`keyword`** — SQLite FTS5 / BM25 keyword search, works with stock
  `sqlite3`, no embeddings required
- **`hybrid`** — reciprocal-rank fusion of both, degrading gracefully to
  keyword-only when `moeka[vec]` isn't installed

Schema changes are versioned via `PRAGMA user_version`; a `meta` table records
the active embedding model + dimension, so switching `embeddingModel`
triggers an automatic re-embed of all four stores from their stored text. An
opt-in retrieval-audit log (`vec.log_retrievals`) records every search
(query, store, returned chunks) to a `retrieval_log` table for observability.
All public `VecStore` methods degrade to empty/no-op — never raise — when
sqlite-vec or sentence-transformers are unavailable.

This is distinct from the Dream/Consolidator flow below: Dream curates
*durable* memory files by editing them; the vec store makes those same files
(plus host documents) *semantically searchable* without waiting for a Dream
pass. See [`docs/core-architecture.md`](core-architecture.md) for the full
design of `nanobot/core/`.

## The Flow

Memory moves through nanobot in two stages.

### Stage 1: Consolidator

When a conversation grows large enough to pressure the context window, nanobot does not try to carry every old message forever.

Instead, the `Consolidator` summarizes the oldest safe slice of the conversation and appends that summary to `memory/history.jsonl`.

This file is:

- append-only
- cursor-based
- optimized for machine consumption first, human inspection second

Each line is a JSON object:

```json
{"cursor": 42, "timestamp": "2026-04-03 00:02", "content": "- User prefers dark mode\n- Decided to use PostgreSQL"}
```

It is not the final memory. It is the material from which final memory is shaped.

### Stage 2: Dream

`Dream` is the slower, more thoughtful layer. It runs on a cron schedule by default and can also be triggered manually.

Dream reads:

- new entries from `memory/history.jsonl`
- the current `SOUL.md`
- the current `USER.md`
- the current `memory/MEMORY.md`

Then it works in two phases:

1. It studies what is new and what is already known.
2. It edits the long-term files surgically, not by rewriting everything, but by making the smallest honest change that keeps memory coherent.

This is why nanobot's memory is not just archival. It is interpretive.

## The Files

```text
workspace/
├── SOUL.md              # The bot's long-term voice and communication style
├── USER.md              # Stable knowledge about the user
└── memory/
    ├── MEMORY.md        # Project facts, decisions, and durable context
    ├── history.jsonl    # Append-only history summaries
    ├── .cursor          # Consolidator write cursor
    ├── .dream_cursor    # Dream consumption cursor
    └── .git/            # Version history for long-term memory files
```

These files play different roles:

- `SOUL.md` remembers how nanobot should sound.
- `USER.md` remembers who the user is and what they prefer.
- `MEMORY.md` remembers what remains true about the work itself.
- `history.jsonl` remembers what happened on the way there.

## Why `history.jsonl`

The old `HISTORY.md` format was pleasant for casual reading, but it was too fragile as an operational substrate.

`history.jsonl` gives nanobot:

- stable incremental cursors
- safer machine parsing
- easier batching
- cleaner migration and compaction
- a better boundary between raw history and curated knowledge

You can still search it with familiar tools:

```bash
# grep
grep -i "keyword" memory/history.jsonl

# jq
cat memory/history.jsonl | jq -r 'select(.content | test("keyword"; "i")) | .content' | tail -20

# Python
python -c "import json; [print(json.loads(l).get('content','')) for l in open('memory/history.jsonl','r',encoding='utf-8') if l.strip() and 'keyword' in l.lower()][-20:]"
```

The difference is philosophical as much as technical:

- `history.jsonl` is for structure
- `SOUL.md`, `USER.md`, and `MEMORY.md` are for meaning

## Commands

Memory is not hidden behind the curtain. Users can inspect and guide it.

| Command | What it does |
|---------|--------------|
| `/dream` | Run Dream immediately |
| `/dream-log` | Show the latest Dream memory change |
| `/dream-log <sha>` | Show a specific Dream change |
| `/dream-restore` | List recent Dream memory versions |
| `/dream-restore <sha>` | Restore memory to the state before a specific change |

These commands exist for a reason: automatic memory is powerful, but users should always retain the right to inspect, understand, and restore it.

## Versioned Memory

After Dream changes long-term memory files, nanobot can record that change with `GitStore`.

This gives memory a history of its own:

- you can inspect what changed
- you can compare versions
- you can restore a previous state

That turns memory from a silent mutation into an auditable process.

## Configuration

Dream is configured under `agents.defaults.dream`:

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "intervalH": 2,
        "modelOverride": null,
        "maxBatchSize": 20,
        "maxIterations": 10
      }
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `intervalH` | How often Dream runs, in hours |
| `modelOverride` | Optional Dream-specific model override |
| `maxBatchSize` | How many history entries Dream processes per run |
| `maxIterations` | The tool budget for Dream's editing phase |

In practical terms:

- `modelOverride: null` means Dream uses the same model as the main agent. Set it only if you want Dream to run on a different model.
- `maxBatchSize` controls how many new `history.jsonl` entries Dream consumes in one run. Larger batches catch up faster; smaller batches are lighter and steadier.
- `maxIterations` limits how many read/edit steps Dream can take while updating `SOUL.md`, `USER.md`, and `MEMORY.md`. It is a safety budget, not a quality score.
- `intervalH` is the normal way to configure Dream. Internally it runs as an `every` schedule, not as a cron expression.

Legacy note:

- Older source-based configs may still contain `dream.cron`. nanobot continues to honor it for backward compatibility, but new configs should use `intervalH`.
- Older source-based configs may still contain `dream.model`. nanobot continues to honor it for backward compatibility, but new configs should use `modelOverride`.

## In Practice

What this means in daily use is simple:

- conversations can stay fast without carrying infinite context
- durable facts can become clearer over time instead of noisier
- the user can inspect and restore memory when needed

Memory should not feel like a dump. It should feel like continuity.

That is what this design is trying to protect.
