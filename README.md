<div align="center">

<img src="./images/moeka-hero.svg" alt="Moeka, the relationship layer: it remembers what you want, briefs a stronger agent, follows up and brings the result back." width="100%">

**The persistent point of contact for your work. Strong agents do the hard part; Moeka keeps your intent from getting lost.**

[Get started](#get-started) · [What Moeka is](#what-moeka-is) · [Why Moeka](#why-moeka) · [Use cases](#put-moeka-to-work) · [Experimental RSI](#the-frontier-experimental-rsi) · [Documentation](#explore-moeka)

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square)
![Self-hosted](https://img.shields.io/badge/Runtime-self--hosted-6C63FF?style=flat-square)
![Experimental RSI](https://img.shields.io/badge/RSI-experimental-FFB86B?style=flat-square)
[![MIT License](https://img.shields.io/badge/License-MIT-55DDB3?style=flat-square)](./LICENSE)

</div>

Moeka is the one assistant you keep talking to. It remembers what you want, briefs a stronger agent such as Claude Code or Codex, follows up, and brings the result back to your browser, terminal or chat. It does not need to be the smartest agent in the loop. It needs to be the one that never loses your intent between you and the agents that do the technical work.

**The ambition: make every conversation useful today, and turn useful work into reusable capability for tomorrow.** Run one personal assistant, a team of specialized instances, or embed the same agent engine in your own application.

## What Moeka is

Moeka is a **relationship layer**. You talk to one familiar assistant. It holds the context, chooses whom to ask, and keeps track of outstanding work. The coding agent figures out how to do the work.

```mermaid
flowchart LR
    You["You"] <--> Moeka["MOEKA<br/>intent · memory · open work"]
    Moeka <--> Adapter["Worker adapter<br/>start · follow up · inspect · stop"]
    Adapter <--> Worker["Claude Code · Codex<br/>other agents and tools"]
    Worker <--> Work["Your projects<br/>and machines"]
    classDef main fill:#6c63ff,color:#fff,stroke:#9892ff
    class Moeka main
```

| Moeka owns | The worker owns |
|---|---|
| Your intent and relevant history | Repository investigation |
| Project constraints and preferences | Technical planning |
| Choosing whom to ask | Code changes |
| The conversation with you | Running tests and debugging |
| Outstanding work | Explaining technical blockers |
| Bringing questions back to you | A reviewable result |

- **Intent comes first.** Moeka forwards your request faithfully with the constraints you gave it. When it is unsure, it hands the worker the question, not an invented plan.
- **Moeka watches and tells you.** The target is: acknowledge your request, ask when unsure, answer status on request, and message you on its own when work finishes or fails. Every request already gets one durable job record (`/jobs`), and a failure names which side broke and why. The proactive finish message is not built yet.
- **Reports are not proof.** When all Moeka has is a worker's report, it says "Codex reports that…" and keeps the commit or artifact reference. Merging, deploying and other consequential changes stay yours to approve.
- **Workers keep their own limits.** A stronger agent does not silently get broader access than the task needs. Moeka's file wall has separate read and write grants, `.env` files stay unreadable, and Moeka cannot rewrite its own source.
- **You can always roll it back.** `/stable` marks a known-good version. `/revert` returns to it, and a boot guard does so on its own after repeated failed starts.

**Where this stands.** The runtime, memory, job record, access grants, owner revert and the Wonhee bridge are built. Delegated workers exist today as spawned sub-agents and CLI apps. A session-based adapter for Claude Code and Codex, which stores explicit session IDs and resumes the right one, is the next piece of the plan ([central plan](./specs/015-central-plan/spec.md)). Moeka's core can keep improving, as long as it keeps doing this job.

## Why Moeka

| Advantage | What it unlocks |
|---|---|
| **Context that compounds** | Durable conversations, explicit memory tools, and Dream consolidation preserve useful knowledge across sessions. Spend less time re-explaining your work. |
| **Delegation that holds together** | Durable job records, ask-when-unsure, status on request and a message when work finishes mean you stop having to ssh in to find out what happened. Files, shell, web research, MCP integrations and delegated workers take a request to an artifact. |
| **Work that keeps moving** | Sustained goals and scheduled tasks support work beyond a single reply. A persistent gateway keeps configured channels and automations online after clients close. |
| **An assistant team you own** | Independent instances have their own personality, memory, credentials, schedules, and permissions. Pair them to exchange authenticated work requests locally or over HTTPS. |
| **Freedom to choose the brain** | Hosted providers and local OpenAI-compatible models share one runtime. Opt-in Luna/GPU routing offers task-level selection with bounded fallback when the GPU is unavailable. |
| **Capabilities worth keeping** | Reusable skills and script tools capture working methods. Certificate-verified transfers let owners move approved capabilities between instances without moving private conversations or credentials. |
| **A foundation for your products** | The embeddable Python kernel exposes agents, structured model calls, document memory, branching sessions, budgets, and traces through one public API. |

**Own the workspace. Choose the models. Build the workflow. Keep the capability.**

## One engine, three ways to build

```mermaid
flowchart TB
    You["Your ideas, projects, and recurring work"] --> Entry["Browser · Terminal · Telegram · API"]
    Entry --> Runtime["MOEKA · Agent runtime"]
    Runtime --> Personal["PERSONAL ASSISTANT<br/>Memory · Tools · Goals · Schedules"]
    Runtime --> Team["CONNECTED INSTANCES<br/>Specialists · Peer requests · Portable skills"]
    Runtime --> Build["APPLICATION ENGINE<br/>Python kernel · Documents · Budgets · Traces"]
    Personal --> Results["Working code · Research briefs · Documents · Delivered results"]
    Team --> Results
    Build --> Results
    classDef main fill:#6c63ff,color:#fff,stroke:#9892ff
    classDef result fill:#143c35,color:#d5fff0,stroke:#55ddb3
    class Runtime main
    class Results result
```

Start with one assistant. Add specialized instances when your work needs them. Use the kernel when you want the same capabilities inside a product. Moeka connects personal productivity, agent coordination, and application development in one stack.

## Put Moeka to work

These are example workflows to configure and request, with the relevant tools and integrations enabled.

| Use it as… | Ask it to… | What makes it useful |
|---|---|---|
| **A daily working partner** | “Remember this project's constraints. Help me plan the next milestone and remind me on Friday.” | Persistent memory, ongoing sessions, and scheduled reminders. |
| **A coding delegator** | “Fix the failing export in this project, then tell me what changed and what you checked.” | Moeka remembers the project and your constraints, hands the work to a stronger agent, and returns the summary, checks and artifact. |
| **A research and document assistant** | “Compare these sources and files, then write a brief with the key tradeoffs.” | Web search and fetch, document processing, and reusable notes. |
| **A recurring operations assistant** | “Check this project every morning and send me a status update.” | Cron automation, a long-running gateway, and channel delivery. |
| **A creative project coordinator** | “Send these references to the paired creative agent and bring its output back here.” | The optional Wonhee bridge connects selected Telegram conversations to a paired creative instance with durable intake and delivery tracking. |
| **An agent-powered application backend** | Build a document assistant, coaching workflow, or task-specific agent inside your own product. | An embeddable kernel with scoped memory, host-supplied tools, usage accounting, and tracing. |

### Your agents can specialize without losing their independence

Give one instance your everyday work, another a focused project, and another an experimental role. Each keeps its own state and access rules. Owner-paired peers can request work from each other while the recipient uses its own tools and permissions.

Reusable capabilities can travel separately: preview a certified bundle, stage it, approve it, and activate it at the appropriate boundary. Transfer history and rollback make adoption reviewable. Your assistant's personality and private memory stay with that assistant.

[Daily assistants](./docs/daily-agents.md) · [Multiple instances](./docs/multiple-instances.md) · [Pairing agents](./docs/guides/paired-agents.md) · [Capability transfer](./docs/capability-transfer.md)

## The frontier: experimental RSI

**What if an agent could improve the way it works—and make those improvements reusable?**

Moeka's experimental **recursive self-improvement** system explores that possibility through a companion harness, **moeka-rsi**. The direction is ambitious: use agent experience to develop better skills, tools, prompts, and operating variants, evaluate candidates, and feed accepted capabilities into future work.

<img src="./images/moeka-rsi.svg" alt="Experimental RSI concept: observe work, develop a candidate, evaluate in isolation, review and adopt, then reuse. Moeka supplies the kernel; the companion harness owns experimentation and evaluation." width="100%">

The advantage is bigger than any single answer: **a useful method can become a capability, and a capability can become a building block for the next task.** This is the research bet behind Moeka's improvement loop.

The repository already supplies concrete building blocks:

- **Controlled experiments:** candidate variants, agent fingerprints, run limits, tool policies, and traceable model/tool activity.
- **Independent checks:** a held-out test runner and a Docker sandbox mode for evaluation outside the candidate's working context.
- **Reusable capabilities:** certificate verification, destination permission checks, staged transfers, activation history, and rollback.
- **Observable cost:** model-call accounting, budgets, and traces for inspecting how an agent spends its resources.

**RSI is experimental.** The companion harness owns candidate development, evaluation, and signing; this repository supplies the runtime and integration surfaces. The loop above describes the research direction, not a claim of demonstrated autonomous improvement. Capability promotion is manual, automatic promotion is off, and core changes require reviewed patches. Daily Moeka assistants run independently of RSI.

[RSI system overview and status](./specs/004-rsi-harness/spec.md) · [Kernel architecture](./docs/core-architecture.md) · [Certified capability transfers](./docs/capability-transfer.md)

## Get started

Install this Moeka checkout to get the features described here. You need **Python 3.11+**, access to a model provider or local model server, and **Bun** for the source WebUI and native terminal client.

```bash
git clone https://github.com/Muxite/moeka.git
cd moeka
python -m venv .venv
```

Activate the environment with `source .venv/bin/activate` on macOS/Linux or `.venv\Scripts\Activate.ps1` in Windows PowerShell, then:

```bash
python -m pip install -e .
moeka webui
```

In **Settings → Models**, configure your provider and model, start a conversation, and choose the intended workspace before asking Moeka to work on a project.

| Next step | Command or guide |
|---|---|
| Work in the terminal | `moeka` |
| Send a single request | `moeka -m "Help me plan my next project"` |
| Keep channels and automations running | `moeka gateway --background` |
| Check the background gateway | `moeka gateway status` |
| Create a named assistant from the checkout | `bin/moeka.sh new research` |
| Configure independent assistants and Luna/GPU routing | [Daily-agent setup](./docs/daily-agents.md) |
| Embed Moeka in an application | [Python SDK](./docs/python-sdk.md) |

Telegram is included. Additional chat integrations install on demand; Discord is also available with `python -m pip install -e ".[discord]"`. Provider credentials, local inference, peer pairing, and RSI each need their own configuration.

## Explore Moeka

| Build on… | Read… |
|---|---|
| **Your assistant** | [Daily agents](./docs/daily-agents.md) · [Memory](./docs/memory.md) · [Automations](./docs/automations.md) |
| **Your interface** | [WebUI](./docs/webui.md) · [Chat apps](./docs/chat-apps.md) · [CLI reference](./docs/cli-reference.md) |
| **Your operator** | [Operating Moeka: jobs, access and revert](./docs/operator.md) · [Operations](./docs/OPERATIONS.md) · [Chat commands](./docs/chat-commands.md) |
| **Your agent team** | [Multiple instances](./docs/multiple-instances.md) · [Peer messaging](./docs/guides/paired-agents.md) · [Capability transfer](./docs/capability-transfer.md) |
| **Your model stack** | [Providers](./docs/providers.md) · [Provider cookbook](./docs/provider-cookbook.md) · [Configuration](./docs/configuration.md) |
| **Your application** | [Python SDK](./docs/python-sdk.md) · [OpenAI-compatible API](./docs/openai-api.md) · [Kernel architecture](./docs/core-architecture.md) |
| **Your deployment** | [Deployment](./docs/deployment.md) · [Development](./docs/development.md) · [Troubleshooting](./docs/troubleshooting.md) |

<sub>MIT licensed. Built on nanobot; upstream attribution is retained in [LICENSE](./LICENSE) and [third-party notices](./THIRD_PARTY_NOTICES.md). The `nanobot` command remains a compatibility alias, and some detailed guides retain upstream naming.</sub>
