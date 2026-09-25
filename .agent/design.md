# Design Constraints

These rules govern architectural decisions. When adding a feature or fixing a bug, prefer paths that respect these boundaries.

## Core stays small; extend at the edges

New capabilities should be added via `tools/`, skills, or MCP servers (the `core-slim` branch has no `channels/`). The files `agent/loop.py` and `agent/runner.py` form the critical core path; changes there should be minimal and justified. If a feature can live in a tool, a skill, or an external MCP server, it should not be inlined into the agent loop.

Runtime state fan-out follows the same boundary. `MessageBus.publish` awaits local subscribers for turn/run/model/goal state changes; `MessageBus.publish_event` queues routed channel delivery without waiting for network sends. Both carry `AgentEvent` values. Runner hooks publish typed output through the turn's scoped `EventSink`; direct-call callbacks are adapted at the execution boundary. Front-end wire details (the WebUI/WebSocket coordinators of the full distribution on `main`) do not belong in the core; `core-slim` has none.

## Less structure, more intelligence

Prefer simple, readable code over new framework layers and indirection. Add structure only when it removes real complexity, protects an important boundary, or matches an established local pattern. The best fix is often a smaller prompt, a tighter tool contract, a tool-local change, or one focused regression test.

## Prefer duplication over premature abstraction

Providers are allowed to repeat similar logic (retries, request shaping, response parsing). Do not introduce complex base classes or shared helpers just to eliminate duplication across provider files. Each provider file should remain self-contained and readable on its own.

## Minimal change that solves the real problem

Fix bugs by changing only what is necessary. Do not bundle unrelated refactors or clean-ups into a feature or bugfix PR. If a refactor is genuinely required, it should be a separate, clearly scoped PR.

## Keep PRs reviewable

A bugfix should make the protected invariant clear, change the smallest surface that enforces it, and add only the closest regression test. If a diff starts changing ownership boundaries or mixing behavior changes with clean-up, split it before it becomes hard to review.

## Type dynamic boundaries at the edge

Wire payloads, persisted records, and third-party SDK objects are untrusted dynamic boundaries. Prefer a parser or small normalizer at the owning edge, and use `TypedDict` for stable dictionary shapes, so validation happens once and internal code receives a concrete type. Do not spread raw dynamic dictionaries or SDK objects through the core.

Stable first-party dependencies must be typed where they are stored or passed. Do not declare an internal service, context field, or callback result as `Any` and then recover its real type with consumer-side casts. Use the concrete type or a narrow `Protocol`; reserve `Any` for genuinely dynamic boundaries.

`typing.cast` performs no runtime validation. Every new cast must be supported by a runtime check on the same path or by an explicit invariant that is clear from construction and control flow (and documented locally when it is not obvious). If input can violate the claimed type, handle that invalid case before casting; never use `cast` only to silence BasedPyright.

## Explicit over magical

Configuration must be declared explicitly in `config/schema.py` Pydantic models. Error handling should raise clear exceptions rather than silently correcting bad input. Provider auto-detection exists, but every resolution path must be traceable from the factory to the concrete provider class.
