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
- **Multi-angle review (T2):** `Workflow({name: "code-review-multiangle", args: {target: "<PR# or branch>"}})`. 8 angles: 7 Claude + 1 **independent-model (codex) angle** (the `grill-me` adversary, run read-only inside the gate) so the review is not Claude-on-Claude (which shares blind spots — see `memory/feedback_independent_model_review_gate`). It **fails CLOSED** — read the result's `status` FIRST:
  - `status: "INCOMPLETE"` (a finder angle failed) or `PREP_FAILED` / `EMPTY_DIFF` / `INVALID_ARGS` ⇒ NOT a valid pass; re-run. Never read a failed run as "0 findings".
  - check `codex_used`: `true` = a real second model attacked the diff; `false` = the codex angle fell back to Claude-only (codex missing/unauthed on the host) ⇒ weaker pass, no independent model actually ran — re-run where codex is available (`codex login`) before treating a clean T2 result as final.
  - sanity-check the reported `base` matches the real PR base (prep resolves it).
  - every `UNVERIFIED` finding (a verifier failed) is preserved at `major` and needs human review.
  - resolve every CONFIRMED + PLAUSIBLE-major finding (fix, or defer with written rationale) before merge.
- **Live smoke (T2):** executable harness `scripts/uat/recon_live_smoke.py` (zero-residue, UAT-tenant-guarded). It logs in to the dedicated `uat-smoke` staging tenant, seeds a tiny canonical set, drives create-run → approve-bucket over the **live HTTP API**, asserts the HITL invariants (per-line + bulk audit by `correlation_id`, no NetSuite post, variance unchanged, `needs_review`→400), then deletes everything (run CASCADE + audit by `correlation_id` + an absolute tenant-wide backstop + seed rows) and asserts **zero residue**. Run it post-deploy:
  ```bash
  export UAT_SMOKE_EMAIL=... UAT_SMOKE_PASSWORD=...    # dedicated uat-smoke tenant, from ~/.hermes/.env
  backend/.venv/bin/python scripts/uat/recon_live_smoke.py \
    --backend-url https://api-staging.suitestudio.ai \
    --database-url "$DATABASE_URL_DIRECT"             # the target's DIRECT url (not the pooler)
  ```
  Exit `0` == full pass + verified zero residue. The hard slug-guard refuses any tenant whose `slug != uat-smoke` and it NEVER `close_period`s. **NEVER point it at a real tenant** — the absolute backstop deletes ALL of a tenant's recon runs/audit, which is safe ONLY on the recon-empty `uat-smoke` fixture. Runbook + safety details: `scripts/uat/README.md`.
- **Seeded-tenant CI e2e (T2):** `backend/tests/e2e/test_recon_lifecycle_e2e.py` against the CI Postgres (the recon write-path regression backbone; = Phase 2).

> Self-review does NOT substitute for the T2 multi-angle review: a self-review once mis-framed a real period-close-integrity bug as intended; the independent multi-angle pass caught it. Spec: `docs/superpowers/specs/2026-06-04-uat-review-process-design.md`.
