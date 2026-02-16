# Audit Logging
_Last updated: 2026-02-15_

## Objectives
- Provide finance-grade evidence: who/what/when/inputs/outputs.
- Enable reproducibility of reconciliation and AI-assisted actions.

## Event categories (minimum)
- Auth & role changes
- Connections (OAuth connect/refresh/disconnect)
- Sync jobs (start/end, cursors, counts, errors, retries)
- Copilot tool requests → approvals → executions
- Reconciliation runs and per-finding evidence pointers
- Change Requests lifecycle
- Writeback executions (if enabled)

## Storage
- Append-only audit table with: timestamp, tenant_id, actor_id, category, action, payload_json
- Include `correlation_id` and `job_id` where applicable
- Optional tamper-evidence via hash chaining

## Access
- Admin and authorized auditors only
- Exportable for close/audit support
