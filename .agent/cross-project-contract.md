# Cross-project contract (pointer)

moeka, awork and awork-resume share one head-agent contract. Source of truth (versioned in awork-resume):
`/home/muk/projects/awork-resume/docs/superpowers/specs/2026-09-30-system-contract.md`.

For moeka: consumers are awork (via a compat shim over the legacy `nanobot.api` entry points; migration
map in `docs/migration-moeka-api.md`), the live gateway (`main`), and the planned RSI harness.
awork-resume does NOT use moeka (owner decision 2026-09-30). TODO: add an assumptions and risks section
to `moeka-kernel-design.md` and RSI design (contract sections 7-8); clarification-yield has no trace event.
