# Cross-project contract (pointer)

moeka, awork and awork-resume share one head-agent contract. Source of truth (versioned in awork-resume):
`/home/muk/projects/awork-resume/docs/superpowers/specs/2026-09-30-system-contract.md` (owner rulings,
sections 3 and 3a). This file only records what binds moeka. Updated 2026-09-30.

- Consumers: awork (via a compat shim over the legacy `nanobot.api` entry points; migration map in
  `docs/migration-moeka-api.md`), the live gateway (`main`), and the planned RSI harness.
- awork-resume is independent of moeka (owner decision 2026-09-30): own `awr.llm`, no moeka code. Kernel
  changes need not consider it.
- Usage and spend come from moeka (owner ruling 2026-09-30): awork derives its usage figures from moeka so
  its interface can show usage and moeka plugs in. The requirement (U1-U12), the dated current state and the
  gaps are in `moeka-kernel-design.md` section 3b. Gaps are not implemented. Until they are, awork keeps its
  `UsageLedger` and `BudgetHalt` mapping over `BudgetExceeded`.
- "Everything is data": better models are fine when every token is attributable and wasted tokens are
  measured (kernel design section 3b, RSI design section 9).
- Verification independence: a verifier runs in a separate context with an evidence-only view; same model is
  acceptable, same agent is an anti-pattern (kernel design section 7).
- Pin decided (owner, 2026-09-30, closed): consumers pin `core-slim` commit `6f80c392` (contains `5b9c7d43`
  plus the upstream sync) until consolidation moves pins to `main`. Do not rebase `core-slim`.
- Consolidation: chosen, not executed: kernel inside moeka `main`, gateway on top as a consumer (kernel design
  section 12 lists what a selective merge must preserve).
- References: kernel design section 11 and RSI design section 17 (verified versus unverified leads).
- Open: assumptions and risks sections exist in the kernel design (section 14) and the RSI design (section 16);
  the clarification-yield trace event is still missing (kernel design section 12).
