# Observability Implementation
_Last updated: 2026-02-16_

This document defines the observability setup for Phase 1: structured logging, correlation ID propagation, metrics, tracing stubs, and runbook templates.

---

## 1. Structured Logging with structlog

### Configuration

```python
# app/core/logging.py
import structlog
import logging

def configure_logging(app_env: str = "development"):
    """Configure structlog for the application."""

    shared_processors = [
        structlog.contextvars.merge_contextvars,        # Merge context variables (tenant_id, etc.)
        structlog.stdlib.add_log_level,                 # Add log level
        structlog.stdlib.add_logger_name,               # Add logger name
        structlog.processors.TimeStamper(fmt="iso"),    # ISO 8601 timestamps
        structlog.processors.StackInfoRenderer(),       # Stack info on exceptions
        structlog.processors.UnicodeDecoder(),          # Decode bytes to strings
    ]

    if app_env == "production":
        # JSON output for production (machine-parseable)
        renderer = structlog.processors.JSONRenderer()
    else:
        # Console output for development (human-readable)
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
```

### Usage Pattern

```python
import structlog

logger = structlog.get_logger()

# In API handler
logger.info(
    "connection_created",
    tenant_id=str(tenant_id),
    connection_id=str(connection.id),
    provider=connection.provider,
    correlation_id=correlation_id,
)

# In worker task
logger.info(
    "sync_job_started",
    tenant_id=tenant_id,
    job_id=job_id,
    job_type="shopify_sync",
    connection_id=connection_id,
    correlation_id=correlation_id,
)

# On error
logger.error(
    "sync_job_failed",
    tenant_id=tenant_id,
    job_id=job_id,
    correlation_id=correlation_id,
    error=str(exc),
    exc_info=True,
)
```

---

## 2. Correlation ID Flow

### Generation

```python
# app/core/middleware.py
import uuid
from starlette.middleware.base import BaseHTTPMiddleware

CORRELATION_ID_HEADER = "X-Correlation-ID"

class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Use existing correlation ID or generate new one
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())

        # Bind to structlog context for all logs in this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        # Store on request state for downstream use
        request.state.correlation_id = correlation_id

        response = await call_next(request)

        # Return in response headers
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
```

### Propagation Chain

```
Client Request
  |  X-Correlation-ID: <uuid>  (generated if absent)
  v
+--------------------------+
| CorrelationIdMiddleware   |  Sets request.state.correlation_id
|                           |  Binds to structlog contextvars
|                           |  Echoes in response header
+-----------+--------------+
            v
+--------------------------+
| API Handler               |  Passes correlation_id to services
|                           |  Includes in audit events
+-----------+--------------+
            v
+--------------------------+
| Celery Task               |  Receives correlation_id in kwargs
| (InstrumentedTask)        |  Sets on Job record
|                           |  Includes in audit events
+-----------+--------------+
            v
+--------------------------+
| MCP Tool Call             |  Receives correlation_id in context
| (Governance Wrapper)      |  Includes in audit log
+--------------------------+
```

### Implementation Points

1. **Middleware** (`core/middleware.py`): Generates or extracts correlation ID from `X-Correlation-ID` header
2. **Auth Dependency** (`core/dependencies.py`): Binds `tenant_id` and `user_id` to structlog contextvars
3. **Audit Service** (`services/audit_service.py`): Accepts `correlation_id` parameter on every event
4. **Celery Tasks** (`workers/base_task.py`): Extracts `correlation_id` from task kwargs, stores on Job record
5. **MCP Governance** (`mcp/governance.py`): Passes `correlation_id` through governance pipeline

### Worker Context Setup

```python
@celery_app.task(bind=True)
def sync_shopify_orders(self, job_id, tenant_id, connection_id, correlation_id):
    # Bind correlation ID to structlog context for this task
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        job_id=job_id,
        celery_task_id=self.request.id,
    )

    logger.info("worker_task_started", job_type="shopify_sync")
    # ... task logic ...
    logger.info("worker_task_completed", rows_processed=count)
```

---

## 3. Log Format and Required Fields

### Standard Fields (Every Log Line)

| Field | Source | Required | Example |
|-------|--------|----------|---------|
| `timestamp` | structlog TimeStamper | Yes | `2026-02-16T14:30:00.123Z` |
| `level` | structlog add_log_level | Yes | `info`, `warning`, `error` |
| `event` | First positional arg | Yes | `connection_created` |
| `correlation_id` | Context var (middleware) | Yes | `a1b2c3d4-...` |
| `tenant_id` | Context var (auth middleware) | Yes* | `550e8400-...` |
| `user_id` | Context var (auth middleware) | No | `6ba7b810-...` |
| `logger` | structlog add_logger_name | Yes | `app.api.v1.connections` |

*`tenant_id` may be absent for unauthenticated endpoints (login, register, health).

### Context-Specific Fields

| Context | Additional Fields |
|---------|-------------------|
| API request | `user_id`, `method`, `path`, `status_code`, `duration_ms` |
| Worker task | `job_id`, `job_type`, `connection_id`, `celery_task_id` |
| MCP tool call | `tool_name`, `execution_time_ms`, `rows_returned` |
| Auth event | `user_id`, `email`, `action` (login/logout/register) |
| Error | `error`, `error_type`, `exc_info` (stack trace) |

### Example Log Output (Production JSON)

```json
{
  "timestamp": "2026-02-16T14:30:00.123456Z",
  "level": "info",
  "event": "sync_job_completed",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "component": "worker",
  "job_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "job_type": "shopify_sync",
  "connection_id": "d4f5a678-1234-5678-9abc-def012345678",
  "celery_task_id": "abc123def456",
  "rows_processed": 150,
  "rows_created": 145,
  "rows_updated": 5,
  "duration_ms": 4523
}
```

### Log Levels Guide

| Level | Usage |
|-------|-------|
| `DEBUG` | Detailed diagnostic info (DB queries, token parsing) -- development only |
| `INFO` | Normal operations (request handled, job started, tool called) |
| `WARNING` | Recoverable issues (rate limit hit, entitlement denied, deprecation) |
| `ERROR` | Failures requiring attention (DB connection lost, decryption failed) |
| `CRITICAL` | System-level failures (cannot start, all workers down) |

### Sensitive Field Redaction

The following fields must never appear in logs:

| Field | Action |
|-------|--------|
| Passwords | Never logged |
| OAuth tokens / API keys | Never logged |
| Encryption keys | Never logged |
| `encrypted_credentials` | Never logged |
| Customer PII (beyond email) | Not logged in Phase 1 |

---

## 4. OpenTelemetry Stub Configuration

Phase 1 configures OpenTelemetry SDK with a no-op or console exporter. This establishes the instrumentation points for when a real backend (Jaeger, Datadog, etc.) is added.

### Setup

```python
# app/core/tracing.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.sdk.resources import Resource

def configure_tracing(app_env: str = "development"):
    """Configure OpenTelemetry tracing (stub for Phase 1)."""

    resource = Resource.create({
        "service.name": "ecom-netsuite-suite",
        "service.version": "0.1.0",
        "deployment.environment": app_env,
    })

    provider = TracerProvider(resource=resource)

    if app_env == "development":
        # Console exporter for local development
        processor = SimpleSpanProcessor(ConsoleSpanExporter())
        provider.add_span_processor(processor)
    # Production: add OTLP exporter when backend is provisioned
    # processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="..."))

    trace.set_tracer_provider(provider)

tracer = trace.get_tracer("ecom-netsuite-suite")
```

### Instrumentation Points

| Component | Instrumentation | Priority |
|-----------|----------------|----------|
| FastAPI | Request/response spans | P0 |
| SQLAlchemy | Query spans | P0 |
| Celery | Task execution spans | P1 |
| Redis | Cache operation spans | P2 |
| MCP | Tool call spans | P1 |
| httpx | Outbound HTTP spans | P1 |

### Example Instrumentation

```python
# API endpoint tracing
@router.post("/connections")
async def create_connection(request: Request, payload: ConnectionCreate):
    with tracer.start_as_current_span("create_connection") as span:
        span.set_attribute("tenant_id", str(request.state.tenant_id))
        span.set_attribute("provider", payload.provider)
        # ... handler logic ...

# Worker task tracing
def sync_shopify_orders(self, job_id, tenant_id, connection_id, correlation_id):
    with tracer.start_as_current_span("sync_shopify_orders") as span:
        span.set_attribute("job_id", job_id)
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("job_type", "shopify_sync")
        # ... task logic ...
```

### Trace Context Propagation

| Hop | Mechanism |
|-----|-----------|
| Browser -> API | `traceparent` header (W3C Trace Context) |
| API -> Worker | Correlation ID in task args (trace context in Celery headers when OTEL is fully enabled) |
| API -> MCP | `traceparent` header in internal HTTP call |

---

## 5. Metric Names

### API Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `api_requests_total` | Counter | `method`, `path`, `status_code`, `tenant_id` | Total HTTP requests |
| `api_request_duration_seconds` | Histogram | `method`, `path`, `status_code` | Request latency |
| `api_auth_failures_total` | Counter | `reason` (`invalid_token`, `expired`, `missing`) | Authentication failures |
| `api_permission_denied_total` | Counter | `permission`, `role` | RBAC denials |
| `api_active_connections` | Gauge | `tenant_id` | Active WebSocket/SSE connections |

### Worker Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `worker_jobs_total` | Counter | `job_type`, `status` (`completed`, `failed`) | Total jobs processed |
| `worker_job_duration_seconds` | Histogram | `job_type` | Job execution time |
| `worker_job_retries_total` | Counter | `job_type` | Retry count |
| `worker_rows_processed_total` | Counter | `job_type`, `operation` (`created`, `updated`) | Rows upserted |
| `worker_queue_depth` | Gauge | `queue_name` | Pending tasks in queue |
| `worker_active_tasks` | Gauge | `worker_id` | Currently executing tasks |

### MCP Tool Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `mcp_tool_invocations_total` | Counter | `tool_name`, `status` (`success`, `error`, `denied`) | Tool call count |
| `mcp_tool_duration_seconds` | Histogram | `tool_name` | Tool execution time |
| `mcp_tool_rows_returned` | Histogram | `tool_name` | Rows in tool response |
| `mcp_tool_rate_limited_total` | Counter | `tool_name`, `tenant_id` | Rate limit hits |

### Connection Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `external_api_requests_total` | Counter | `provider`, `endpoint`, `status_code` | Calls to external APIs |
| `external_api_duration_seconds` | Histogram | `provider`, `endpoint` | External API latency |
| `external_api_rate_limited_total` | Counter | `provider` | External rate limit hits |

### Business Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `tenants_active_total` | Gauge | `plan` | Active tenants by plan |
| `connections_active_total` | Gauge | `provider` | Active connections by provider |
| `sync_freshness_seconds` | Gauge | `tenant_id`, `provider`, `object_type` | Seconds since last successful sync |
| `entitlement_denials_total` | Counter | `plan`, `feature` | Entitlement check failures |
| `audit_events_total` | Counter | `category`, `action` | Audit events emitted |

### Metric Collection

Metrics exposed via Prometheus-compatible endpoint at `/metrics` using `prometheus_client` or OpenTelemetry metrics SDK:

```python
# app/core/metrics.py
from prometheus_client import Counter, Histogram, Gauge

api_requests = Counter(
    "api_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

api_latency = Histogram(
    "api_request_duration_seconds",
    "Request latency in seconds",
    ["method", "path", "status_code"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

worker_jobs = Counter(
    "worker_jobs_total",
    "Total jobs processed",
    ["job_type", "status"],
)
```

---

## 6. Runbook Templates

### Runbook 1: Connection Authentication Failure

**Alert:** `external_api_requests_total{status_code="401"}` increases

**Symptoms:**
- Sync jobs failing with authentication errors
- Connection status changed to `error`
- Audit events: `job_failed` with `error_message` containing "401" or "Unauthorized"

**Investigation Steps:**

1. Identify affected tenant and connection:
   ```sql
   SELECT j.tenant_id, j.connection_id, c.provider, c.label, j.error_message
   FROM jobs j JOIN connections c ON j.connection_id = c.id
   WHERE j.status = 'failed'
     AND j.error_message ILIKE '%401%'
     AND j.created_at > now() - interval '1 hour';
   ```

2. Check if credentials were recently rotated on the provider side (out-of-band change).

3. Check `encryption_key_version` matches current key:
   ```sql
   SELECT id, provider, label, encryption_key_version
   FROM connections WHERE id = '<connection_id>';
   ```

4. Verify the connection using the test endpoint:
   ```
   POST /api/v1/connections/<connection_id>/test
   ```

**Resolution:**
- If credentials expired: notify tenant admin to reconnect.
- If encryption key mismatch: run key re-encryption procedure.
- If provider-side change: guide tenant to update credentials.

---

### Runbook 2: Worker Queue Backlog

**Alert:** `worker_queue_depth` > 100 for > 5 minutes

**Symptoms:**
- Jobs stuck in `pending` status
- Sync freshness degrading (`sync_freshness_seconds` increasing)
- Users reporting stale data

**Investigation Steps:**

1. Check worker process health:
   ```bash
   celery -A app.workers inspect active
   celery -A app.workers inspect reserved
   celery -A app.workers inspect stats
   ```

2. Check Redis broker connectivity:
   ```bash
   redis-cli -u $CELERY_BROKER_URL ping
   redis-cli -u $CELERY_BROKER_URL llen celery
   ```

3. Check for stuck tasks (running too long):
   ```sql
   SELECT id, job_type, status, started_at, now() - started_at as duration
   FROM jobs
   WHERE status = 'running' AND started_at < now() - interval '10 minutes';
   ```

4. Check if a specific job type is causing the backlog:
   ```sql
   SELECT job_type, count(*) FROM jobs
   WHERE status = 'pending' GROUP BY job_type ORDER BY count DESC;
   ```

**Resolution:**
- If workers are down: restart worker processes.
- If Redis is down: restart Redis, check persistence settings.
- If stuck tasks: investigate and potentially revoke them.
- If volume spike: scale workers horizontally.

---

### Runbook 3: External API Rate Limiting

**Alert:** `external_api_rate_limited_total` increases

**Symptoms:**
- Sync jobs taking longer than usual
- Retry counts increasing
- Audit events showing rate-limit related errors

**Investigation Steps:**

1. Identify which provider and tenant:
   ```sql
   SELECT j.tenant_id, c.provider, count(*) as failed_count
   FROM jobs j JOIN connections c ON j.connection_id = c.id
   WHERE (j.error_message ILIKE '%rate%limit%' OR j.error_message ILIKE '%429%')
     AND j.created_at > now() - interval '1 hour'
   GROUP BY j.tenant_id, c.provider;
   ```

2. Check current sync frequency for the tenant.

3. Review external API response headers for rate limit details (Retry-After, X-RateLimit-Remaining).

**Resolution:**
- Increase backoff intervals for the affected provider.
- Reduce sync frequency for the tenant.
- If persistent: contact provider about rate limit increases.
- Implement request coalescing if multiple jobs hit the same API.

---

### Runbook 4: High API Latency

**Alert:** `api_request_duration_seconds` p95 > 2 seconds for > 5 minutes

**Symptoms:**
- Frontend showing slow load times
- Users reporting timeouts
- Health check endpoint still responsive

**Investigation Steps:**

1. Identify slow endpoints:
   ```
   Check metrics: api_request_duration_seconds by path
   ```

2. Check database connection pool:
   ```sql
   SELECT count(*) FROM pg_stat_activity WHERE datname = 'ecom_netsuite';
   ```

3. Check for long-running queries:
   ```sql
   SELECT pid, now() - query_start as duration, query
   FROM pg_stat_activity
   WHERE state = 'active' AND query_start < now() - interval '5 seconds'
   ORDER BY duration DESC;
   ```

4. Check table sizes and index usage:
   ```sql
   SELECT relname, n_live_tup, seq_scan, idx_scan
   FROM pg_stat_user_tables
   ORDER BY n_live_tup DESC;
   ```

**Resolution:**
- If missing indexes: add indexes (see DATA_MODEL_OVERVIEW.md for index strategy).
- If connection pool exhausted: increase pool size or investigate connection leaks.
- If large table scans: verify RLS policies use indexed `tenant_id`.
- If specific query: analyze with `EXPLAIN ANALYZE` and optimize.

---

### Runbook 5: Tenant Data Isolation Concern

**Alert:** Manual report or security audit finding

**Symptoms:**
- Suspicion that one tenant may be seeing another tenant's data
- Audit log anomaly (unexpected tenant_id in events)

**Investigation Steps:**

1. Verify RLS is enabled on all tables:
   ```sql
   SELECT tablename, rowsecurity
   FROM pg_tables
   WHERE schemaname = 'public' AND tablename LIKE 'canonical_%';
   ```

2. Test RLS enforcement:
   ```sql
   -- As app_user role (not superuser)
   SET LOCAL app.current_tenant_id = '<tenant_a_id>';
   SELECT count(*) FROM canonical_orders;  -- should return only tenant A rows

   SET LOCAL app.current_tenant_id = '<tenant_b_id>';
   SELECT count(*) FROM canonical_orders;  -- should return only tenant B rows
   ```

3. Check for any queries running without tenant context:
   ```sql
   SELECT * FROM audit_events
   WHERE tenant_id IS NULL AND timestamp > now() - interval '24 hours';
   ```

4. Review application logs for missing `tenant_id` in log lines.

**Resolution:**
- If RLS disabled on a table: enable immediately and investigate how it was disabled.
- If queries running without context: patch the code path, add middleware enforcement.
- If confirmed data leak: initiate incident response procedure, notify affected tenants.

---

### Runbook 6: Encryption Key Issues

**Alert:** Connection creation/reading failures with decryption errors

**Symptoms:**
- `InvalidToken` exceptions in worker logs
- Connection test failures
- Audit events with `error_message` containing "decryption" or "InvalidToken"

**Investigation Steps:**

1. Verify `ENCRYPTION_KEY` env var is set and is a valid Fernet key.
2. Check `encryption_key_version` on affected connections:
   ```sql
   SELECT id, provider, encryption_key_version
   FROM connections WHERE status = 'error';
   ```
3. If key was recently rotated, ensure the previous key is still available for decryption of not-yet-re-encrypted rows.
4. Review audit events for `connection_created` or `connection_tested` failures.

**Resolution:**
- If key env var missing: restore from secrets manager.
- If version mismatch after rotation: run re-encryption migration.
- If key compromised: rotate immediately, re-encrypt all connections, revoke and re-issue affected credentials.

---

## 7. Dashboard Layout (Future)

### Overview Dashboard
- Request rate and error rate
- P50/P95/P99 latency
- Active tenants by plan
- Job queue depth and throughput

### Tenant Dashboard
- Per-tenant request volume
- Connection health status
- Recent audit events
- Job success/failure ratio
- Sync freshness per provider

### Worker Dashboard
- Task throughput by queue
- Worker utilization
- Failed task trend
- Queue backlog size
- Retry rate by job type
