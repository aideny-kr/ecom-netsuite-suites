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

## Dev Workspace Runner and Testing Pipeline

The Dev/Admin Workspace adds an IDE-like filesystem layer and a runner-backed execution pipeline.

### Components
- **Workspace Service (Virtual FS):** stores/imports an SDF-style project snapshot and exposes file operations (list/read/search) for the IDE UI and chat references (`@workspace:/path`).  
  Spec: `DEV_WORKSPACE_IDE_FS.md`

- **Change Set Service:** all edits are represented as diff-based Change Sets with an approval state machine. No direct edits to the baseline snapshot.  
  Spec: `DEV_WORKSPACE_IDE_FS.md`

- **Runner Service (Privileged Execution):** executes allowlisted commands in isolated per-tenant workspaces and produces immutable artifacts (logs/reports).  
  Spec: `RUNNER_SERVICE.md`

- **Runs + Artifacts Model:** every validate/test/deploy/assertion operation is a Run record linked to audit events and artifact references.  
  Spec: `DEV_WORKSPACE_RUNS.md`

- **SuiteQL Assertions (Integration Smoke Checks):** read-only, governed queries executed against sandbox to validate key invariants before deploy; produces an auditable report.  
  Spec: `SUITEQL_ASSERTIONS.md`

### Privileged operations and gating
Privileged actions (validate/tests/deploy/apply_patch) must be:
- tenant-isolated and RBAC protected
- approval-gated at the Change Set level
- fully auditable with correlation_id and artifact references

### Tooling
Runner allowlisted commands only:
- `suitecloud project:validate`
- `jest` (SuiteCloud Unit Testing)
- `suitecloud project:deploy` (sandbox only in beta)

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

## Dev/Admin Workspace IDE components
Add these components for the Dev/Admin MVP:

- **Workspace Service (Virtual FS):** stores/imports SDF project files, exposes list/read/search APIs.
- **Change Set Service:** stores diffs, approvals, and state machine for edits.
- **MCP IDE Tools:** filesystem-like tools (`list_files`, `read_file`, `search`, `propose_patch`, `apply_patch`) used by chat and IDE UI.
- **Artifact Store:** immutable logs and reports (validate/tests/deploy later), linked from audit events.

See: `DEV_WORKSPACE_IDE_FS.md`.
