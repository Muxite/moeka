# Gateway parity probes (2026-10-01)

Read-only probes that compare an OLD checkout (`main` before cutover) with a CANDIDATE checkout
(`consolidate/new-main`) without touching the live service, the live checkout or the real `~/.nanobot`.

Run each probe once per checkout, with `PYTHONPATH` pointing at that checkout and any venv that has the
gateway dependencies (the live venv works, nothing is installed or written to it). Always set a throwaway
`HOME` and `PYTHONDONTWRITEBYTECODE=1`.

```bash
export S=$(mktemp -d); export HOME=$S/home; mkdir -p $HOME
cp ~/.nanobot/config.json $S/cfg.json            # copy taken BEFORE HOME is switched
PY=/path/to/venv/bin/python
for T in /path/old-checkout /path/candidate-checkout; do
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$T S=$S $PY gateway_parity.py "$T" > $S/$(basename $T).json
done
diff <(python3 -m json.tool $S/old-checkout.json) <(python3 -m json.tool $S/candidate-checkout.json)
```

`gateway_parity.py` builds the same `AgentLoop` the gateway builds (`host_tools=True` when the checkout
accepts it) from a COPY of the live config and prints: the registered tool names, the exec child
environment keys, the system prompt, and whether the config validates. `stub_openai.py` is a local
OpenAI-compatible endpoint that records requests, so the exact provider request (headers, body, tools)
of two versions can be diffed offline with no paid call.

Result on 2026-10-01 (old `54069bb2`, candidate `bc86a31f`): tools identical except `defer_action`
(new); exec env keys identical; channel plugin list identical; the live config validates on both and
is rejected by `core-slim` (extra `channels`, `api`, `gateway`); provider request headers identical and
body identical except for `defer_action` in the tool list and about 200 bytes of system-prompt text.
