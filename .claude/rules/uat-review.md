---
description: e2e UAT + review depth required per change, decided by risk tier. The canonical tiering checklist is always-on in CLAUDE.md; this rule is the how-to-run detail.
paths:
  - backend/app/**
  - backend/tests/**
  - backend/alembic/**
  - frontend/src/**
  - suiteapp/**
  - .github/workflows/**
  - .claude/workflows/**
  - scripts/**
---

# UAT + Review — risk-tier policy (how-to-run)

> The TIERING CHECKLIST is also in CLAUDE.md ("## UAT + Review") so it loads on EVERY PR — including ones that don't touch the paths above. This rule is the detail for running each gate.

## Trigger checklist — ANY one makes a PR **T2 (high-risk)**
- Mutates customer data (create / update / delete / approve / lock / post)
- HITL invariant (per-line audit, no-auto-post, approval gating, period freeze)
- Financial period close / lock / unlock, money movement, variance / materiality
- Auth / permissions / RLS / tenant-scoping
- Alembic migration or schema change
- Secrets / encryption / API keys / credential handling
- Scheduled / cron / Beat jobs (InstrumentedTask)
- Deploy / runtime infra (compose, Dockerfile, CI/CD, nginx)
- Prompt-pollution surface: chat/agent prompts, knowledge profiles, golden datasets, SSE number interception
- Soul config (`/tmp/workspace_storage/{tenant_id}/soul.md`)
- File-cabinet I/O or MCP mutation writes
- Key-billed chat/agent path
- **Changes to the review/UAT process tooling or policy itself** — a buggy gate gives false confidence on everything it gates (this is why this very file is T2).

**T1 (standard)** = a code change hitting NONE of the above. **T0 (trivial)** = docs / comments / formatting / lint-config / rename ONLY. Dependency upgrades and auth/deploy/feature-flag/scheduler config are NOT T0 — tier them by the triggers above.

## Gates per tier
| Tier | CI | Live smoke (post-deploy) | Review |
|------|----|--------------------------|--------|
| T0 | existing CI | — | optional light |
| T1 | existing CI + e2e if it touches a covered flow | — | `/code-review` (light) |
| T2 | existing CI + **mandatory seeded-tenant e2e** | **PM-autonomous safe-envelope live smoke** | **mandatory multi-angle review, pre-merge, blocking** |

## How to run each gate
- **Multi-angle review (T2):** `Workflow({name: "code-review-multiangle", args: {target: "<PR# or branch>"}})`. It fails CLOSED — check `failed_angles` (NON-EMPTY ⇒ NOT a clean pass; re-run the review) and treat every `UNVERIFIED` finding as needing human review (a verifier failed, the candidate was preserved). Resolve every CONFIRMED + PLAUSIBLE-major finding (fix, or defer with written rationale) before merge.
- **Live smoke (T2):** safe-envelope harness — mint an env-scoped token on the target backend → create a FRESH DISPOSABLE run → exercise → verify by `correlation_id`/audit → DELETE (verify zero residue). Default target: the staging UAT test-tenant; real-tenant disposable run only as fallback. NEVER auto-mutate a real close-bound run; NEVER `close_period` a real period. (Executable harness = Phase 3.)
- **Seeded-tenant CI e2e (T2):** `backend/tests/e2e/` against the CI Postgres. (= Phase 2.)

> Self-review does NOT substitute for the T2 multi-angle review: a self-review once mis-framed a real period-close-integrity bug as intended; the independent multi-angle pass caught it. Spec: `docs/superpowers/specs/2026-06-04-uat-review-process-design.md`.
