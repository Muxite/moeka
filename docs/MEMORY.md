# Memory in nanobot

> **Note:** This design is currently an experiment in the latest source code version and is planned to officially ship in `v0.1.5`.

nanobot's memory is built on a simple belief: memory should feel alive, but it should not feel chaotic.

Good memory is not a pile of notes. It is a quiet system of attention. It notices what is worth keeping, lets go of what no longer needs the spotlight, and turns lived experience into something calm, durable, and useful.

That is the shape of memory in nanobot.

## The Design

nanobot does not treat memory as one giant file.

It separates memory into layers, because different kinds of remembering deserve different tools:

- `session.messages` holds the living short-term conversation.
- `memory/history.jsonl` is the running archive of compressed past turns.
- `SOUL.md`, `USER.md`, and `memory/MEMORY.md` are the durable knowledge files.
- `GitStore` records how those durable files change over time.

This keeps the system light in the moment, but reflective over time.

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

```
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

## Vector Memory (Moeka Extension)

Moeka extends the base memory system with a semantic search layer powered by
[sqlite-vec](https://github.com/asg017/sqlite-vec) and
[sentence-transformers](https://www.sbert.net/).  This is a Moeka-only feature
and is not present in upstream nanobot.

### What it does

When enabled, Moeka maintains a local SQLite database (`memory/vec.db`) that
stores 384-dimensional embeddings for three sources:

| Index | Content | When updated |
|-------|---------|--------------|
| `memory` | Chunks of `MEMORY.md`, split on `## ` headings | After every Dream run |
| `history` | Each `history.jsonl` entry | On every history append |
| `skills` | Skill name + first-line description from `SKILL.md` | On startup if count changes |

The embedding model is loaded lazily — only when `memory_search` is first called.

### The `memory_search` tool

When vector memory is enabled, a new tool becomes available to the agent:

```
memory_search(query, k=5, scope="all")
```

| Parameter | Meaning |
|-----------|---------|
| `query` | Natural-language description of what to recall |
| `k` | Number of results to return (1–20, default 5) |
| `scope` | `"all"`, `"memory"`, `"history"`, or `"skills"` |

The tool returns results ranked by cosine similarity.  Use it for fuzzy recall
("what did we decide about the database schema?").  Use `grep` for
exact-text searches.

### Setup

Install the optional dependency group:

```bash
pip install "moeka[vec]"
# or: pip install sqlite-vec sentence-transformers
```

Enable in your config (`~/.nanobot/config.json`):

```json
{
  "agents": {
    "defaults": {
      "vectorMemory": {
        "enabled": true,
        "modelName": "all-MiniLM-L6-v2",
        "topK": 5,
        "chunkSize": 512
      }
    }
  }
}
```

| Field | Meaning | Default |
|-------|---------|---------|
| `enabled` | Turn vector memory on/off | `false` |
| `modelName` | sentence-transformers model to use | `"all-MiniLM-L6-v2"` |
| `topK` | Default number of results from `memory_search` | `5` |
| `chunkSize` | Max chars per `MEMORY.md` chunk | `512` |

The first run downloads the model (~90 MB) from Hugging Face.  All subsequent
runs load it from the local sentence-transformers cache.

### Architecture

```
MemoryStore (file I/O)          VectorMemoryStore
  MEMORY.md  ──── chunks ──────>  memory_chunks_vec
  history.jsonl ── entries ─────>  history_entries_vec
  skills/*/SKILL.md ─ descs ───>  skills_vec
                                       │
                                  MemorySearchTool
                                       │
                                  Agent tool calls
```

### Comparison: grep vs. memory_search

| | `grep` | `memory_search` |
|-|--------|-----------------|
| Match type | Exact regex | Semantic / fuzzy |
| Best for | Known keywords, code, IDs | Concepts, paraphrased facts |
| Index | Filesystem | SQLite vec0 table |
| Speed | Fast | ~100–500 ms (model inference) |

Both are always available when vector memory is enabled.

---

## In Practice

What this means in daily use is simple:

- conversations can stay fast without carrying infinite context
- durable facts can become clearer over time instead of noisier
- the user can inspect and restore memory when needed

Memory should not feel like a dump. It should feel like continuity.

That is what this design is trying to protect.
