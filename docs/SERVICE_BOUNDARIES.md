# Service Boundaries
_Last updated: 2026-02-16_

This document defines process boundaries, responsibilities, and data flow between the three runtime services in Phase 1: API Server, Worker, and MCP Server.

---

## Architecture Overview

```
                    +-----------+
                    |  Next.js  |
                    |  Frontend |
                    +-----+-----+
                          |
                          | HTTPS (JWT)
                          |
                    +-----v-----+          +-------------+
                    |  FastAPI   |---Redis-->|   Celery    |
                    |  API       |<--Redis---|   Worker    |
                    |  Server    |          +------+------+
                    +-----+-----+                 |
                          |                       |
                          |              +--------v--------+
                          |              | External APIs   |
                          |              | (NS/Shopify/    |
                          |              |  Stripe)        |
                    +-----v-----+        +-----------------+
                    | MCP Server |
                    | (AI Tools) |
                    +-----+-----+
                          |
                    +-----v-----+
                    | PostgreSQL |
                    |  (RLS)    |
                    +-----------+
```

---

## Service 1: API Server (FastAPI)

### Responsibilities

| Responsibility | Details |
|---------------|---------|
| Authentication | JWT issuance (access + refresh), password verification, token refresh |
| Authorization | RBAC middleware checks permissions before handler execution |
| Tenant Context | Sets `SET LOCAL app.current_tenant_id` on every DB session for RLS |
| Connection CRUD | Create, read, test, revoke connections (credentials encrypted/decrypted) |
| Tenant Config | CRUD for tenant configuration (subsidiaries, account mappings, posting policy) |
| Table Queries | Paginated, sorted, filtered reads from canonical tables |
| Export | CSV/Excel generation based on table queries + entitlement checks |
| Job Dispatch | Creates `jobs` row + dispatches Celery task |
| Audit Reads | Paginated, filtered read access to audit_events |
| Entitlement Enforcement | Checks `tenants.plan` + limits before gated operations |
| Correlation ID | Generates `correlation_id` per request, passes to response headers and downstream |

### Does NOT Do

- Long-running data processing (offloaded to Worker)
- Direct external API calls for sync (offloaded to Worker)
- AI model interaction (offloaded to MCP Server)
- Background scheduling (offloaded to Celery Beat)

### Key Middleware Stack

```
Request
  |-> Correlation ID middleware (generate/extract X-Correlation-ID)
  |-> JWT authentication middleware (validate token, extract user/tenant)
  |-> Tenant context middleware (SET LOCAL app.current_tenant_id)
  |-> RBAC authorization middleware (check permission codename)
  |-> Rate limiting middleware (per-tenant, per-endpoint)
  |-> Handler execution
  |-> Structured logging (tenant_id, user_id, correlation_id in every log)
Response
```

### Database Session Pattern

```python
# Every API request sets tenant context before any query
async with get_db_session() as session:
    await session.execute(
        text("SET LOCAL app.current_tenant_id = :tid"),
        {"tid": str(tenant_id)}
    )
    # All subsequent queries in this session are RLS-filtered
```

---

## Service 2: Worker (Celery)

### Responsibilities

| Responsibility | Details |
|---------------|---------|
| Sync Jobs | Fetch data from external APIs (NetSuite, Shopify, Stripe) and upsert into canonical tables |
| Job Lifecycle | Update `jobs` row: pending -> running -> completed/failed |
| Audit Emission | Emit audit events for job_started, job_completed, job_failed |
| Idempotent Processing | UPSERT by dedupe key; safe to retry |
| Cursor Management | Track incremental sync cursors per connection |
| Error Handling | Exponential backoff + jitter for rate limits; dead-letter for repeated failures |
| Correlation ID | Receive and propagate correlation_id from job parameters into all logs and audit events |

### Does NOT Do

- Serve HTTP requests
- Authenticate users
- Enforce RBAC (trusts the API server that dispatched the job)
- Handle AI tool calls

### Task Registration

```python
# All tasks registered under app.workers.tasks.*
@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,          # re-deliver if worker crashes
    reject_on_worker_lost=True,
)
def sync_shopify_orders(self, job_id: str, tenant_id: str, connection_id: str, correlation_id: str):
    ...
```

### Tenant Context in Worker

Workers set tenant context on their own DB sessions:

```python
# Worker establishes its own DB session with tenant context
with get_sync_session() as session:
    session.execute(
        text("SET LOCAL app.current_tenant_id = :tid"),
        {"tid": tenant_id}
    )
    # Process rows with RLS enforced
```

### Job State Machine

```
              dispatch
  [pending] ---------> [running]
                          |
                    +-----+-----+
                    |           |
               [completed]  [failed]
                               |
                          (retry?) --> [running]  (up to max_retries)
                               |
                          [dead_letter]
```

---

## Service 3: MCP Server (AI Tool Interface)

### Responsibilities

| Responsibility | Details |
|---------------|---------|
| Tool Registry | Expose typed tool definitions with parameter schemas |
| Governance | Enforce per-tool limits: default LIMIT, max rows, allowlisted tables, timeout, rate limit |
| Parameter Validation | Validate all tool inputs against JSON Schema before execution |
| Approval Gating | Block write-operation tools unless explicit approval is present |
| Audit Emission | Log every tool invocation (inputs, outputs, status, timing) to audit_events |
| Stub Responses | Phase 1: return stub responses; real implementations in Phase 2+ |
| Tenant Context | Receive tenant_id from caller; set RLS context for any DB queries |
| Correlation ID | Receive and propagate correlation_id |

### Does NOT Do

- Authenticate users directly (trusts the API server or orchestrator)
- Manage connections or credentials
- Dispatch background jobs
- Serve the frontend UI

### Tool Manifest Structure

```json
{
  "tools": [
    {
      "name": "run_suiteql",
      "description": "Execute a SuiteQL query against NetSuite",
      "parameters": {
        "query": {"type": "string", "required": true},
        "limit": {"type": "integer", "default": 100, "maximum": 1000}
      },
      "governance": {
        "default_limit": 100,
        "max_rows": 1000,
        "timeout_seconds": 30,
        "rate_limit_per_minute": 20,
        "requires_approval": false,
        "allowlisted_tables": ["transaction", "account", "customer", "item", "subsidiary"]
      }
    }
  ]
}
```

---

## Inter-Service Communication

### API -> Worker (Job Dispatch)

```
API Server                    Redis (Broker)                Celery Worker
    |                              |                              |
    |-- celery.send_task() ------->|                              |
    |   (job_id, tenant_id,        |                              |
    |    connection_id,             |                              |
    |    correlation_id)            |                              |
    |                              |-- deliver task -------------->|
    |                              |                              |-- process job
    |                              |                              |-- update jobs table
    |                              |                              |-- emit audit events
    |                              |<-- result (optional) --------|
    |                              |                              |
```

**Data passed via task arguments (not HTTP):**
- `job_id` (UUID): reference to the `jobs` row
- `tenant_id` (UUID): for RLS context in worker
- `connection_id` (UUID): which connection to use for external API calls
- `correlation_id` (string): for log/audit correlation

### API -> MCP Server

```
API Server                     MCP Server
    |                              |
    |-- HTTP POST /tools/invoke -->|
    |   {tool, params,             |
    |    tenant_id, user_id,       |
    |    correlation_id}           |
    |                              |-- validate params
    |                              |-- check governance
    |                              |-- execute (stub in Phase 1)
    |                              |-- emit audit event
    |<-- {result, metadata} -------|
    |                              |
```

The MCP server runs as a separate process, reachable from the API server via internal HTTP on port `MCP_SERVER_PORT` (default 8001). In Phase 1, it can also be embedded as a module within the API server process.

---

## Correlation ID Propagation

The correlation ID flows through every layer to enable end-to-end tracing of a logical operation.

```
Browser Request
  |
  | X-Correlation-ID: <uuid>  (generated by API if absent)
  |
  v
API Server
  |-- logs: {correlation_id: <uuid>, tenant_id, user_id, ...}
  |-- audit_events: {correlation_id: <uuid>, ...}
  |-- job dispatch: correlation_id passed as task argument
  |
  +---> Worker
  |       |-- logs: {correlation_id: <uuid>, tenant_id, job_id, ...}
  |       |-- audit_events: {correlation_id: <uuid>, ...}
  |
  +---> MCP Server
          |-- logs: {correlation_id: <uuid>, tenant_id, tool_name, ...}
          |-- audit_events: {correlation_id: <uuid>, ...}
```

**Rules:**
1. If the incoming request has `X-Correlation-ID`, use it.
2. Otherwise, generate a new UUID v4.
3. Return the `X-Correlation-ID` in the response headers.
4. Pass the correlation_id to every downstream call (task args, internal HTTP headers).
5. Include the correlation_id in every structured log line and audit event.

---

## Data Flow: Sync Job (End-to-End)

```
1. User clicks "Run Sync" for Shopify connection
   |
2. Frontend: POST /api/v1/jobs {job_type: 'shopify_sync', connection_id}
   |
3. API Server:
   a. Authenticate (JWT) and authorize (RBAC: jobs:write)
   b. Check entitlement (trial: limited syncs)
   c. Create jobs row (status=pending, correlation_id generated)
   d. Dispatch Celery task with (job_id, tenant_id, connection_id, correlation_id)
   e. Emit audit event: job_dispatched
   f. Return 202 {job_id, correlation_id}
   |
4. Celery Worker picks up task:
   a. Update job status to 'running', set started_at
   b. Emit audit event: job_started
   c. Decrypt connection credentials (Fernet)
   d. Fetch data from Shopify API (paginated, incremental cursor)
   e. Transform to canonical schema
   f. UPSERT into canonical tables by dedupe key
   g. Update sync cursor
   h. On success: update job status to 'completed', set result_summary
   i. Emit audit event: job_completed {rows_processed, rows_created, rows_updated}
   j. On failure: update job status to 'failed', set error_message
   k. Emit audit event: job_failed {error}
   |
5. User polls GET /api/v1/jobs/{job_id} or receives WebSocket update
   |
6. User navigates to Tables to see the synced data
```

---

## Port and Process Summary

| Service | Default Port | Process | Scaling |
|---------|-------------|---------|---------|
| API Server (FastAPI) | 8000 | `uvicorn app.main:app` | Horizontal (multiple workers behind LB) |
| Celery Worker | N/A (pulls from Redis) | `celery -A app.workers worker` | Horizontal (add worker processes) |
| Celery Beat | N/A | `celery -A app.workers beat` | Single instance (leader election) |
| MCP Server | 8001 | `python -m app.mcp.server` | Single instance (Phase 1) |
| PostgreSQL | 5432 | Managed/container | Vertical (Phase 1) |
| Redis | 6379 | Managed/container | Single instance (Phase 1) |
| Next.js Frontend | 3000 | `next start` | CDN + serverless (Vercel) |

---

## Security Boundaries

| Boundary | Enforcement |
|----------|------------|
| Frontend -> API | HTTPS + JWT (access token in Authorization header) |
| API -> Database | RLS via `SET LOCAL app.current_tenant_id` per session |
| API -> Worker | Trusted internal (same network); tenant_id passed explicitly |
| API -> MCP | Trusted internal HTTP; tenant_id + user_id + correlation_id in request body |
| Worker -> Database | RLS via `SET LOCAL` with tenant_id from task args |
| Worker -> External APIs | Decrypted credentials per connection; TLS required |
| MCP -> Database | RLS via `SET LOCAL` with tenant_id from request |
