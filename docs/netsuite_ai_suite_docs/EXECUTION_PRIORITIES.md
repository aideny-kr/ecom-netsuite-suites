# Execution Priorities
_Last updated: 2026-02-16_

## Guiding rule
Bake moat requirements into **Phase 1 constraints** (data model, policies, audit hooks), implement them fully in later phases.

## Phase 1 — Foundation (Week 1–2)
**Goal:** Build the platform that makes finance-grade integrations possible.

Must ship:
- multi-tenant + RBAC + server-side entitlements
- token vault interface (encryption at rest) + rotation plan
- append-only audit events with correlation_id/job_id
- canonical tables scaffold (payouts, payout_lines, orders, refunds, netsuite_postings)
- table-first UI skeleton + CSV export
- MCP tool skeleton with governance hooks (limits, allowlists, audit)

Bake in now (as schemas/policies, even if not active yet):
- subsidiary dimension on canonical objects (where applicable)
- account mapping config model (cash/refund/clearing accounts)

## Phase 2 — Read-only integrations (Week 2–3)
**Goal:** Populate canonical tables and prove visibility.

Must ship:
- NetSuite read-only SuiteQL tool (governed)
- Stripe ingestion: payouts + balance transactions (+ disputes if feasible)
- Shopify ingestion: orders + refunds (+ payments/transactions if needed)
- incremental cursors + deterministic dedupe + retry-safe jobs
- UI: drilldown payout → lines → related objects + sync status

## Phase 3 — Reconciliation MVP + evidence packs (Week 3–4)
**Goal:** Deliver the primary value.

Must ship:
- payout-level matching rules + variance taxonomy
- explainable findings (rule fired, inputs, totals)
- evidence pack export (CSV/Excel + JSON metadata)
- balance check page (read-only): reconciliation totals vs NetSuite account balances (per subsidiary if enabled)
- scheduled email digest (paid gate) for variances

## Phase 4 — Account-aware copilot + scheduled reports (Week 4–5)
**Goal:** Monetizable automation.

Must ship:
- onboarding questions → persist account-specific policy context:
  - subsidiaries enabled?
  - key account mappings (cash/refund/clearing)
  - posting preferences (lumpsum vs detail)
- copilot can create saved reports and schedule them via job scheduler
- cron job system for scheduled emails and report exports (paid)

## Phase 5 — Writeback/posting + evidence attachment (Week 5+)
**Goal:** High trust + high risk feature; only after approvals + idempotency are solid.

Must ship:
- approvals workflow for posting (optional dual control)
- idempotent writeback execution (idempotency keys, safe retries)
- posting modes: lumpsum vs detail (order reference, ids)
- JE batching (≤200 lines per JE batch is a safe default)
- upload evidence CSV into NetSuite File Cabinet and attach to JE
