---
description: e2e UAT + review depth — how to RUN each gate. The canonical tiering CHECKLIST is in CLAUDE.md ("## UAT + Review"), always-on; this rule is loaded when editing code/policy and covers execution detail.
paths:
  - backend/app/**
  - backend/tests/**
  - backend/alembic/**
  - frontend/src/**
  - suiteapp/**
  - .github/workflows/**
  - .claude/workflows/**
  - .claude/rules/**
  - scripts/**
  - CLAUDE.md
---

# UAT + Review — how to run each gate

> **The canonical tiering CHECKLIST (which PRs are T2 / T1 / T0) is in `CLAUDE.md` → "## UAT + Review"** — single source of truth, always loaded on every PR. Do NOT duplicate the checklist here; it drifts. This rule is the execution detail.

## Gates per tier
| Tier | CI | Live smoke (post-deploy) | Review |
|------|----|--------------------------|--------|
| T0 | existing CI | — | optional light |
| T1 | existing CI + e2e if it touches a covered flow | — | `/code-review` (light) |
| T2 | existing CI + **mandatory seeded-tenant e2e** | **PM-autonomous safe-envelope live smoke** | **mandatory multi-angle review, pre-merge, blocking** |

## How to run each gate
- **Multi-angle review (T2):** `Workflow({name: "code-review-multiangle", args: {target: "<PR# or branch>"}})`. It **fails CLOSED** — read the result's `status` FIRST:
  - `status: "INCOMPLETE"` (a finder angle failed) or `PREP_FAILED` / `EMPTY_DIFF` / `INVALID_ARGS` ⇒ NOT a valid pass; re-run. Never read a failed run as "0 findings".
  - sanity-check the reported `base` matches the real PR base (prep resolves it).
  - every `UNVERIFIED` finding (a verifier failed) is preserved at `major` and needs human review.
  - resolve every CONFIRMED + PLAUSIBLE-major finding (fix, or defer with written rationale) before merge.
- **Live smoke (T2):** safe-envelope harness — mint an env-scoped token on the target backend → create a FRESH DISPOSABLE run → exercise → verify by `correlation_id`/audit → DELETE (verify zero residue). Default target: the staging UAT test-tenant; real-tenant disposable run only as fallback. NEVER auto-mutate a real close-bound run; NEVER `close_period` a real period. (Executable harness = Phase 3.)
- **Seeded-tenant CI e2e (T2):** `backend/tests/e2e/` against the CI Postgres. (= Phase 2.)

> Self-review does NOT substitute for the T2 multi-angle review: a self-review once mis-framed a real period-close-integrity bug as intended; the independent multi-angle pass caught it. Spec: `docs/superpowers/specs/2026-06-04-uat-review-process-design.md`.
