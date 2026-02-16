# Phase 1 Acceptance Criteria
_Last updated: 2026-02-16_

This document defines testable acceptance criteria for every Phase 1 exit gate. Each criterion includes specific test steps and expected outcomes. All criteria must pass before Phase 1 is considered complete.

---

## AC-1: Register Tenant, Add Connection, View Empty Canonical Tables

### AC-1.1 Tenant Registration

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | POST `/api/v1/auth/register` with `{name, slug, email, password}` | 201 Created; response contains `tenant_id`, `user_id`, JWT access + refresh tokens |
| 2 | Verify `tenants` row | Row exists with `plan='trial'`, `is_active=true`, `plan_expires_at` set to now + 60 days |
| 3 | Verify `users` row | Row exists with `tenant_id` FK, `hashed_password` is bcrypt hash (not plaintext) |
| 4 | Verify `user_roles` row | User assigned `admin` role automatically |
| 5 | Verify `tenant_configs` row | Row exists with default `posting_mode='lumpsum'`, `posting_batch_size=100` |
| 6 | Verify `audit_events` row | `category='auth'`, `action='tenant_registered'`, `tenant_id` set, `correlation_id` present |
| 7 | Attempt duplicate slug | 409 Conflict returned |

### AC-1.2 Add Connection

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | POST `/api/v1/connections` with provider=`netsuite`, credentials, label | 201 Created; `connection_id` returned |
| 2 | Verify `connections` row | `encrypted_credentials` is not plaintext (Fernet-encrypted), `encryption_key_version=1`, `status='active'` |
| 3 | GET `/api/v1/connections` | List includes the new connection; `encrypted_credentials` field is **redacted** in response (masked or omitted) |
| 4 | Verify audit event | `category='connection'`, `action='connection_created'`, `resource_id` = connection_id |
| 5 | Add second connection (provider=`shopify`) | 201 Created; both connections visible in list |
| 6 | Add connection with invalid provider | 422 Validation error |

### AC-1.3 View Empty Canonical Tables

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | GET `/api/v1/tables/orders` | 200 OK; `data: []`, `total: 0`, pagination metadata present |
| 2 | GET `/api/v1/tables/payouts` | 200 OK; empty result set with correct column schema |
| 3 | GET `/api/v1/tables/refunds` | 200 OK; empty result set |
| 4 | Navigate to Tables page in UI | Empty state message displayed: "No data yet. Run a sync to populate tables." |
| 5 | Verify table columns match schema | Response includes column names, types matching canonical model definition |

---

## AC-2: Workers Run Jobs with Audit Events and Correlation ID

### AC-2.1 Job Creation and Execution

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | POST `/api/v1/jobs` with `{job_type: 'shopify_sync', connection_id}` | 202 Accepted; returns `job_id`, `correlation_id` |
| 2 | Verify `jobs` row | `status='pending'`, `correlation_id` is a valid UUID-format string, `celery_task_id` populated |
| 3 | Wait for Celery worker pickup | `status` transitions to `'running'`, `started_at` set |
| 4 | Job completes | `status='completed'`, `completed_at` set, `result_summary` contains `{rows_processed, rows_created, rows_updated}` |
| 5 | Verify audit trail | At least 2 audit events: `job_started` and `job_completed`, both sharing same `correlation_id` and `job_id` |

### AC-2.2 Job Failure Handling

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Trigger a job that fails (e.g., invalid connection) | Job transitions to `status='failed'` |
| 2 | Verify `error_message` | Contains meaningful error description |
| 3 | Verify audit event | `category='job'`, `action='job_failed'`, `status='error'`, `error_message` populated, same `correlation_id` |
| 4 | Retry the job | New job created with new `job_id` but can reference original via `parameters` |

### AC-2.3 Correlation ID Propagation

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Initiate a sync job via API | API returns `correlation_id` in response headers (`X-Correlation-ID`) |
| 2 | Check worker logs | `correlation_id` appears in every structured log line emitted by the worker for this job |
| 3 | Check audit events | All audit events for this job share the same `correlation_id` |
| 4 | Check job record | `jobs.correlation_id` matches the API-issued value |

---

## AC-3: MCP Tools Exist as Stubs with Governance and Audit

### AC-3.1 Tool Registration

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | List available MCP tools | Returns tool manifest with at least: `run_suiteql`, `export_table`, `list_tables`, `get_job_status` |
| 2 | Each tool has metadata | `name`, `description`, `parameters` schema, `default_limit`, `max_rows`, `timeout_seconds`, `rate_limit_per_minute`, `requires_approval` flag |

### AC-3.2 Stub Execution

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Call `run_suiteql` tool with a valid query | Returns stub response: `{status: 'stub', message: 'Tool not yet implemented', tool: 'run_suiteql'}` |
| 2 | Verify audit event | `category='mcp_tool'`, `action='tool_invoked'`, `resource_type='run_suiteql'`, `payload` contains input parameters, `correlation_id` present |
| 3 | Call tool with parameters exceeding `max_rows` | Rejected with governance error: "Requested rows exceeds maximum of {max_rows}" |
| 4 | Call tool exceeding rate limit | 429 response with retry-after indication |

### AC-3.3 Governance Enforcement

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Call `run_suiteql` without explicit LIMIT | Tool injects `default_limit` (e.g., 100) into query |
| 2 | Call `run_suiteql` with table not in allowlist | Rejected: "Table not in allowlist" |
| 3 | Call a write-operation tool (e.g., `create_journal_entry`) | Rejected unless `requires_approval=true` and approval granted |
| 4 | Verify all rejections are audited | Audit events created with `status='denied'` and denial reason |

---

## AC-4: Tenant Config Has Subsidiaries, Account Mappings, and Posting Policy

### AC-4.1 Config Creation and Defaults

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Register a new tenant | `tenant_configs` row created with defaults: `posting_mode='lumpsum'`, `posting_batch_size=100`, `posting_attach_evidence=false` |
| 2 | GET `/api/v1/tenant/config` | Returns full config including null `subsidiaries`, null `account_mappings` |

### AC-4.2 Config Update

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | PATCH `/api/v1/tenant/config` with subsidiaries JSON | 200 OK; `subsidiaries` field updated |
| 2 | Verify subsidiaries structure | Accepts `[{id, name, currency, is_primary}]` format |
| 3 | PATCH with account_mappings | Accepts `{cash_account, clearing_account, refund_account, fees_account}` per subsidiary |
| 4 | PATCH with `posting_mode='detail'` | `posting_mode` updated to `'detail'` |
| 5 | PATCH with invalid `posting_mode` | 422 Validation error (must be `lumpsum` or `detail`) |
| 6 | Verify audit event | `category='config'`, `action='tenant_config_updated'`, `payload` contains changed fields |

### AC-4.3 Config Isolation

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Tenant A updates config | Tenant A config changed |
| 2 | Tenant B reads config | Tenant B sees only their own config (unchanged) |
| 3 | Tenant A attempts to read Tenant B config by ID | 403 or 404 (RLS prevents access) |

---

## AC-5: RBAC Enforcement (Admin vs Read-Only)

### AC-5.1 Role Assignment

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Admin invites user with `readonly` role | User created with `user_roles` entry pointing to `readonly` role |
| 2 | Verify `roles` table | Contains at least: `admin`, `finance`, `ops`, `readonly` |
| 3 | Verify `permissions` table | Contains codenames like `connections:write`, `connections:read`, `tables:read`, `tables:export`, `config:write`, `users:manage`, `audit:read`, `jobs:write` |

### AC-5.2 Admin Access

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Admin calls POST `/api/v1/connections` | 201 Created |
| 2 | Admin calls PATCH `/api/v1/tenant/config` | 200 OK |
| 3 | Admin calls POST `/api/v1/users/invite` | 201 Created |
| 4 | Admin calls GET `/api/v1/audit` | 200 OK with audit events |
| 5 | Admin calls POST `/api/v1/jobs` | 202 Accepted |

### AC-5.3 Read-Only Access

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Read-only user calls GET `/api/v1/tables/orders` | 200 OK (read allowed) |
| 2 | Read-only user calls POST `/api/v1/connections` | 403 Forbidden |
| 3 | Read-only user calls PATCH `/api/v1/tenant/config` | 403 Forbidden |
| 4 | Read-only user calls POST `/api/v1/users/invite` | 403 Forbidden |
| 5 | Read-only user calls POST `/api/v1/jobs` | 403 Forbidden |
| 6 | Read-only user calls GET `/api/v1/audit` | 403 Forbidden |
| 7 | Verify denial audited | Audit event with `category='auth'`, `action='permission_denied'` |

### AC-5.4 Cross-Tenant Isolation

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Tenant A admin fetches Tenant B resources by UUID | 404 Not Found (RLS blocks visibility) |
| 2 | SQL query without `SET LOCAL` tenant context | Returns zero rows (RLS default deny) |

---

## AC-6: Entitlement Checks (Trial vs Pro)

### AC-6.1 Trial Limits

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Trial tenant adds 2nd external connection (beyond NetSuite + 1 source) | 403 with message: "Trial plan limited to 1 external source connection" |
| 2 | Trial tenant requests Excel export | 403 with message: "Excel export requires Pro plan" |
| 3 | Trial tenant requests CSV export | 200 OK (CSV allowed on trial) |
| 4 | Trial tenant triggers MCP tool call beyond trial limit | 403 with usage count in message |
| 5 | Trial tenant attempts to schedule a job | 403 with message: "Scheduling requires Pro plan" |
| 6 | Verify entitlement denial audited | `category='entitlement'`, `action='entitlement_denied'`, `payload` contains `{plan, feature, limit}` |

### AC-6.2 Pro Access

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Pro tenant adds multiple external connections | 201 Created for each |
| 2 | Pro tenant requests Excel export | 200 OK with .xlsx file |
| 3 | Pro tenant schedules a job | 201 Created |
| 4 | Pro tenant accesses evidence packs | 200 OK |

### AC-6.3 Plan Transition

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Upgrade tenant from `trial` to `pro` | `tenants.plan` updated; `plan_expires_at` cleared or extended |
| 2 | Previously gated features now accessible | Excel export, scheduling return 200 OK |
| 3 | Trial expiry (plan_expires_at in past) | All write operations return 403; read-only access preserved |
| 4 | Verify plan change audited | `category='billing'`, `action='plan_changed'`, `payload` contains old and new plan |

---

## AC-7: Full End-to-End Phase 1 Smoke Test

| Step | Action | Expected Outcome |
|------|--------|------------------|
| 1 | Register tenant "Acme Corp" | Tenant, user, config, admin role all created |
| 2 | Login with credentials | JWT access + refresh tokens returned |
| 3 | Add NetSuite connection | Connection stored encrypted |
| 4 | Add Shopify connection | Second connection stored |
| 5 | Configure subsidiaries and account mappings | Config updated |
| 6 | View tables (empty) | Empty tables with correct schemas |
| 7 | Trigger sync job | Job queued, picked up by worker, audit events created |
| 8 | View audit trail | All events visible, filterable by category |
| 9 | Call MCP tool stub | Stub response + audit event |
| 10 | Invite read-only user | User created with readonly role |
| 11 | Login as read-only user | Can view tables, cannot modify connections/config |
| 12 | Verify all correlation_ids | Every action chain shares consistent correlation_id |

---

## Exit Gate Summary

| Gate | Description | Status |
|------|-------------|--------|
| AC-1 | Register + Connect + View Tables | Pending |
| AC-2 | Workers + Audit + Correlation ID | Pending |
| AC-3 | MCP Stubs + Governance + Audit | Pending |
| AC-4 | Tenant Config (subsidiaries, mappings, posting) | Pending |
| AC-5 | RBAC Enforcement | Pending |
| AC-6 | Entitlement Checks | Pending |
| AC-7 | End-to-End Smoke Test | Pending |
