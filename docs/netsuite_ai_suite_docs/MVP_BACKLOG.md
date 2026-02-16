# MVP Backlog (6 Weeks)
_Last updated: 2026-02-15_

## Epics
1) Tenant foundation (auth, RBAC, entitlements, audit logs)
2) Integrations (NetSuite + Shopify + Stripe)
3) Visibility tables (operational UI + exports)
4) Copilot (tool-gated SuiteQL + report generation)
5) Reconciliation workflow (rules + evidence packs)
6) Admin Copilot + Change Requests
7) Billing + subscriptions
8) Production readiness (observability, retries, security)

## Sprint 1 (Weeks 1–2): Foundations + NetSuite connectivity
- [ ] Multi-tenant schema + RBAC + entitlement checks
- [ ] Token vault/encryption + rotation plan
- [ ] Audit event schema + append-only storage
- [ ] NetSuite connector: SuiteTalk REST base client + SuiteQL tool (read-only)
- [ ] Onboarding wizard: store NETSUITE_ACCOUNT_CONTEXT + validate NetSuite permissions
- [ ] Structured logs with `tenant_id` + `correlation_id`

Acceptance criteria:
- Can connect NetSuite and run a safe SuiteQL query with LIMIT and logging.

## Sprint 2 (Weeks 3–4): Shopify/Stripe + tables + exports
- [ ] Shopify OAuth + ingestion of orders/refunds (minimal objects)
- [ ] Stripe OAuth + ingestion of payouts/balance transactions/disputes (minimal)
- [ ] Canonical tables + dedupe keys + incremental cursors
- [ ] Table UI: payouts/orders/refunds with filters and drill-down
- [ ] CSV export; basic Excel export

Acceptance criteria:
- Customer can see “table view visibility” across sources and filter by date/status.

## Sprint 3 (Weeks 5–6): Reconciliation v1 + Copilot v1 + billing
- [ ] Reconciliation engine: payout-level matching + variance taxonomy
- [ ] Evidence pack export (xlsx/csv + audit summary)
- [ ] Copilot: propose SuiteQL → run tool → render table → export HTML/Excel (gated)
- [ ] Approval workflow stub for any write actions
- [ ] Billing + trial expiry + entitlements
- [ ] Scheduled jobs framework (paid): email digest

Acceptance criteria:
- Run reconciliation and produce evidence pack.
- Copilot produces a finance report with provenance pointers.
- Trial gating works and upgrade unlocks features.

## Post-MVP (High value)
- Slack/Google Chat alerts
- NetSuite bundle + RESTlet-based pull/push optimizations
- Line-level reconciliation and auto-suggested matching clusters
- Admin Copilot: dependency graph + impact analysis automation
- Writeback execution with idempotency keys + approvals

## Moat-driven backlog additions
- [ ] Persist account mapping config (cash/clearing/refund accounts) and subsidiary mode during onboarding
- [ ] Add balance check page (recon totals vs NetSuite balances) per subsidiary
- [ ] Add scheduling framework (scheduler + DB schedules) with paid entitlement gate
- [ ] Define posting policy (lumpsum vs detail) and evidence attachment requirements (implementation later)
