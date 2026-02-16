# Architecture
_Last updated: 2026-02-15_

## Goals
- Multi-tenant, secure, audit-friendly (finance-grade)
- Integrations with NetSuite + Shopify + Stripe
- Background jobs for sync/recon/scheduled reports
- Tool-governed AI actions (SuiteQL, exports, change requests)
- Table-first UX for visibility and operational drill-down

## Documents
- `TENANCY_RBAC.md`
- `SECURITY.md`
- `DATA_PIPELINE_IDEMPOTENCY.md`
- `OBSERVABILITY.md`
- `AUDIT_LOGGING.md`

## High-Level Components
1) UI (Streamlit for rapid MVP, or Next.js for product UI)
2) API Service (FastAPI):
   - Auth, tenant config, RBAC + entitlements
   - Connection management (OAuth tokens)
   - Copilot endpoints (chat sessions, tool calls, CR drafts)
   - Billing webhooks + plan enforcement
3) Worker Service (Celery):
   - ingestion sync pipelines (Shopify/Stripe/NetSuite)
   - reconciliation runs (pandas-heavy)
   - report generation & scheduled tasks
4) MCP Server (Python):
   - typed tool interface exposed to the model
   - tools: SuiteQL, export, recon, CR creation, optional writeback
5) Data stores:
   - Postgres (system of record) + pgvector
   - Redis (Celery broker + cache)
   - Object storage (exports/evidence packs)

## Tenancy model
Recommended v1: `tenant_id` on all rows + Postgres Row Level Security (RLS) enforced in DB and app.

## AI tool governance
- All model actions go through tools.
- Tools enforce:
  - allowlists/denylists
  - default LIMITs + max rows
  - timeouts + rate limits
  - mandatory audit events
- Write tools (posting) require approvals and entitlements.

## Data ingestion
- Incremental cursors per connector
- Deterministic dedupe keys
- Idempotent retry behavior for jobs and writeback

## Observability
- Structured logs + metrics + traces
- Tenant-aware dashboards
- Job-level inspection and replay controls
