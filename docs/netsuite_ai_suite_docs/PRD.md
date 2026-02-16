# NetSuite Ecommerce Ops Suite (Working Name) — PRD
_Last updated: 2026-02-15_

## 1) Summary
A multi-tenant SaaS suite for **NetSuite ecommerce operators** (starting with Shopify + Stripe) that provides:
1) **Visibility-first dashboards and tables** (payouts, orders, refunds, fees, discrepancies) with drill-down.
2) A governed **AI Copilot** that can generate and run **SuiteQL** safely and produce reports (Excel/HTML).
3) An **audit-grade reconciliation** workflow between Shopify/Stripe and NetSuite with evidence packs.
4) A **NetSuite Admin Copilot** that explains customizations/SuiteScripts and drafts **Change Requests (CRs)**.

## 2) Target Users & Personas
### Persona A: NetSuite Admin / Systems Owner
- Owns NetSuite configuration, scripts, roles/permissions.
- Pain: hard to understand customizations/scripts; risky changes; unclear impact.

### Persona B: Controller / Finance Lead
- Owns month-end close and reconciliation.
- Pain: payout reconciliation is time-consuming; lacks traceability and evidence.

### Persona C: Ecommerce Ops Analyst
- Owns daily monitoring of orders, refunds, payouts, failures.
- Pain: poor visibility in middleware; needs table views and fast drill-down.

## 3) Customer Pain Points (Observed)
- Limited “single pane of glass” for payouts vs NetSuite posting status.
- Slow or opaque “why is this payout off?” investigations.
- Hard to see the exact mapping/logic that led to posting outcomes.
- Admins struggle to answer “what custom logic exists?” and “what should we change?”
- BI/reporting requires heavy manual query/report building.

## 4) Jobs-to-be-Done
- **JTBD-1:** “Show me today’s payout discrepancies and why they happened.”
- **JTBD-2:** “Reconcile Stripe/Shopify payouts to NetSuite deposits and produce an evidence pack.”
- **JTBD-3:** “Generate a safe SuiteQL query + report that answers my finance question.”
- **JTBD-4:** “Explain current scripts/customizations affecting cash clearing and draft a change request.”

## 5) MVP Scope (v1)
### 5.1 Onboarding & Connectivity
- Tenant signup + organization settings
- Connect NetSuite + connect Shopify or Stripe (OAuth tokens stored encrypted)
- Ask onboarding questions to build a stable NetSuite context (subsidiary, currency, clearing accounts, etc.)
- Health check: verify permissions and run a minimal SuiteQL query

### 5.2 Visibility UI (Tables first)
- Unified table views:
  - Orders / payments / refunds (Shopify/Stripe)
  - Payouts / settlement lines / fees / disputes
  - NetSuite transactions involved in payout lifecycle (deposits, journal entries, customer deposits, etc.)
  - Sync/recon status with filters and drill-down
- Saved views + exports (CSV + Excel)

### 5.3 Copilot (Governed, Tool-Gated)
- Chat interface that can:
  - propose SuiteQL
  - run SuiteQL via an approved tool with row limits
  - return results + provenance pointers (what tables/objects were used)
  - export reports to Excel/HTML (limited in free tier)

### 5.4 Reconciliation (Rules First)
- Reconcile Stripe/Shopify → NetSuite using deterministic rules + variance taxonomy
- “Explainable findings” (rule fired, matching keys, timing window)
- Evidence pack export (xlsx/csv + audit trail summary)

### 5.5 Admin Copilot + Change Requests
- Index and summarize scripts/custom records/fields relevant to ecommerce + cash posting
- Q&A about existing logic (“what script touches deposit creation?”)
- Generate a Change Request draft:
  - problem statement, proposed change, impact/risk, rollback, test plan
  - suggested script snippet(s) or pseudo-code
- CR workflow: draft → review → approved (no auto-deploy in MVP)

## 6) Non-Goals (MVP)
- Auto-deploying SuiteScripts to production
- Fully automated posting without explicit approval
- Supporting every ecommerce channel or gateway
- Full data warehouse / BI replacement

## 7) Pricing & Packaging (Initial)
- Trial (freemium): 2-month trial with limited usage
- Paid: $399/month or $4,000/year
- Add-ons:
  - Slack/Google Chat notifications
  - “Posting/sync back to NetSuite” capability
  - Consulting: $175/hr for NetSuite-side custom work

## 8) Success Metrics
- Activation: % of trials that connect NetSuite + a source and view key tables
- Time-to-first-value: time to first reconciliation run + evidence export
- Close acceleration: reduction in payout reconciliation time
- Conversion: trial → paid conversion rate
- Retention: WAU/MAU for finance + ops users

## 9) Risks & Mitigations
- Prompt-injection / unsafe tool calls → tool allowlists + approvals + audit logs
- NetSuite permissions variability → onboarding checklist + least-privilege guidance
- Data volume/rate limits → incremental sync + caching + backoff
- Trust barrier (finance) → evidence packs, immutable logs, strict token handling

## 10) Must-Have Platform Components (Worth It)
These are required for a finance/integration SaaS.

### 10.1 Multi-tenancy + RBAC
- Tenant isolation enforced at DB + app layers
- Roles: Admin, Finance, Ops, Read-only
- Server-side entitlements

### 10.2 Token vault + key rotation
- Encrypt OAuth tokens at rest
- Rotate encryption keys
- Minimize connector scopes/roles

### 10.3 Idempotency + safe retries
- Ingestion and writeback must be idempotent
- Every external write uses idempotency keys / natural keys to prevent duplicates
- Support backfills with deterministic dedupe

### 10.4 Audit trail + evidence packs
- Every finding links to raw source lines and rule versions
- Every AI tool call is logged (actor, params, outputs metadata)

### 10.5 Observability + supportability
- Tenant-aware logs, metrics, traces
- Job dashboards and failure inspection

## Moat requirements to bake in
These are product requirements (not just implementation details):

- **AI-assisted reconciliation**: AI suggests likely causes and next steps, but outputs must remain explainable and auditable.
- **Account-aware setup**: onboarding captures account-specific policies (subsidiaries, key accounts, posting preferences) and persists them as configuration used by reconciliation + copilot.
- **Subsidiary-aware balance checks**: show configured balance sheet amounts for cash/clearing/refund accounts (per subsidiary and consolidated where feasible) and explain variances.
- **Posting policy**: customers choose discrepancy posting mode (lumpsum vs detail with ids/order references). Detail posting batches journal lines (default ≤200/JE) and attaches evidence CSV to the journal.
- **Scheduled reports**: chat/report outputs can be scheduled (paid) as emails and exports with full audit logging.
