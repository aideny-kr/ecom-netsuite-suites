---
description: e2e UAT + review depth required per change, decided by risk tier. Loads when editing app code.
paths:
  - backend/app/**
  - backend/tests/**
  - frontend/src/**
  - suiteapp/**
---

# UAT + Review — risk-tier policy

Every PR has a tier. The tier decides the gates. Pick the tier with the trigger checklist.

## Trigger checklist — ANY one makes a PR **T2 (high-risk)**
- Mutates customer data (create / update / delete / approve / lock / post)
- Touches a HITL invariant (per-line audit, no-auto-post, approval gating, period freeze)
- Financial period close / lock / unlock, money movement, variance / materiality
- Auth / permissions / RLS / tenant-scoping
- Alembic migration or schema change
- Chat/agent behavior on the key-billed path

**T1 (standard)** = a code change hitting none of the above. **T0 (trivial)** = docs / config / deps / rename only.

## Gates per tier
| Tier | CI | Live smoke (post-deploy) | Review |
|------|----|--------------------------|--------|
| T0 | existing CI | — | optional light |
| T1 | existing CI + e2e if it touches a covered flow | — | `/code-review` (light) |
| T2 | existing CI + **mandatory seeded-tenant e2e** | **PM-autonomous safe-envelope live smoke** | **mandatory multi-angle review, pre-merge, blocking** |

## How to run each gate
- **Multi-angle review (T2):** `Workflow({name: "code-review-multiangle", args: {target: "<PR# or branch>"}})`. Resolve every CONFIRMED + PLAUSIBLE-major finding (fix, or defer with written rationale) before merge.
- **Live smoke (T2):** safe-envelope harness — mint an env-scoped token on the target backend → create a FRESH DISPOSABLE run → exercise → verify by `correlation_id`/audit → DELETE (verify zero residue). Default target: the staging UAT test-tenant; real-tenant disposable run only as fallback. NEVER auto-mutate a real close-bound run; NEVER `close_period` a real period.
- **Seeded-tenant CI e2e (T2):** `backend/tests/e2e/` against the CI Postgres.

> Self-review does NOT substitute for the T2 multi-angle review: a self-review once mis-framed a real period-close-integrity bug as intended; the independent multi-angle pass caught it. See `docs/superpowers/specs/2026-06-04-uat-review-process-design.md`.
