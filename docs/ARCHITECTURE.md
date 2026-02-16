# Architecture Overview

## System Context

NetSuite Ecommerce Ops Suite sits between ecommerce payment processors (Stripe, Shopify) and NetSuite ERP. It ingests transaction data from processors, normalizes it into a canonical model, reconciles payouts against payments/orders, and posts journal entries to NetSuite.

```
+-----------+     +-------------------------+     +----------+
|  Stripe   |---->|                         |---->| NetSuite |
+-----------+     |   Ecom NetSuite Suite   |     |   ERP    |
+-----------+     |                         |     +----------+
|  Shopify  |---->|  Backend  |  Worker(s)  |
+-----------+     |  (FastAPI)|  (Celery)   |
                  |           |             |
                  |  Frontend | MCP Server  |
                  |  (Next.js)|             |
                  +-------------------------+
                        |           |
                  +-----+-----+    |
                  | PostgreSQL |    |
                  | (pgvector) |    |
                  +------------+    |
                  +--------+       |
                  | Redis  |-------+
                  +--------+
```

## Component Architecture

### Backend (FastAPI)

The backend is a Python FastAPI application organized in a layered architecture:

```
app/
  main.py              # Application factory, middleware registration
  api/v1/              # HTTP route handlers (thin controllers)
    router.py          # Central router aggregating all sub-routers
    auth.py            # Registration, login, token refresh, tenant switching
    tenants.py         # Tenant CRUD and config management
    users.py           # User CRUD and RBAC role assignment
    connections.py     # Encrypted connection management
    tables.py          # Generic canonical table queries and CSV export
    jobs.py            # Background job status
    audit.py           # Audit event log viewer
    health.py          # Liveness/readiness probe
  core/                # Cross-cutting infrastructure
    config.py          # Pydantic settings loaded from .env
    database.py        # AsyncSession factory, engine configuration
    dependencies.py    # Auth dependency (get_current_user), permission/entitlement checkers
    encryption.py      # Fernet encrypt/decrypt for stored credentials
    logging.py         # structlog configuration
    middleware.py      # Correlation ID middleware (X-Correlation-ID header)
    security.py        # JWT encode/decode, password hashing
  models/              # SQLAlchemy ORM models
  schemas/             # Pydantic v2 request/response schemas
  services/            # Business logic layer
  workers/             # Celery app and task definitions
  mcp/                 # MCP tool server
```

### Request Flow

1. HTTP request arrives at FastAPI
2. `CORSMiddleware` handles preflight
3. `CorrelationIdMiddleware` assigns/propagates a correlation ID
4. Route handler invokes `get_current_user` or `require_permission` dependency
5. `get_current_user` decodes the JWT, loads the user, and sets the PostgreSQL RLS context (`SET LOCAL app.current_tenant_id`)
6. `require_permission` additionally checks that the user's roles grant the required permission
7. Handler calls service layer, which interacts with models/database
8. Audit events are logged for significant actions
9. Response is returned

### Multi-Tenancy

The system uses a **shared database, shared schema** multi-tenancy model:

- Every data table includes a `tenant_id` column
- PostgreSQL Row-Level Security (RLS) policies ensure queries only return rows for the current tenant
- The `tenant_id` is set via `SET LOCAL app.current_tenant_id` on each authenticated request
- Application-level checks in `get_current_user` enforce tenant scoping

See [ADR_002_MULTI_TENANCY.md](ADR_002_MULTI_TENANCY.md) for the full decision record.

### RBAC (Role-Based Access Control)

```
User --< UserRole >-- Role --< RolePermission >-- Permission
```

- Users are assigned roles within their tenant via the `UserRole` join table
- Roles have permissions via the `RolePermission` join table
- Permission codenames (e.g., `tenant.manage`, `connections.view`) gate API endpoints
- The `require_permission(codename)` dependency checks permissions on each request

### Plan-Based Entitlements

Tenants have a `plan` field (`trial`, `pro`, `enterprise`) that gates features:

| Feature | Trial | Pro | Enterprise |
|---|---|---|---|
| Connections | 2 | 50 | 500 |
| Table views | yes | yes | yes |
| CSV exports | yes | yes | yes |
| MCP tools | no | yes | yes |

Entitlement checks occur in the `require_entitlement` dependency and the `entitlement_service`.

### Frontend (Next.js)

The frontend is a Next.js 14 application using the App Router:

```
src/
  app/
    layout.tsx           # Root layout with global styles
    login/page.tsx       # Login page
    register/page.tsx    # Registration page
    (dashboard)/         # Authenticated layout group
      layout.tsx         # Dashboard shell with sidebar
      dashboard/page.tsx # Overview dashboard
      connections/       # Connection management
      tables/[tableName] # Data table viewer (dynamic route)
      audit/             # Audit log viewer
  components/
    sidebar.tsx          # Navigation sidebar
    data-table.tsx       # Generic data table (TanStack Table)
    add-connection-dialog.tsx
    audit-filters.tsx
    table-toolbar.tsx
    ui/                  # Radix-based UI primitives (button, card, dialog, etc.)
  hooks/                 # React Query data fetching hooks
  lib/
    api-client.ts        # Fetch wrapper with auth token injection
    types.ts             # TypeScript interfaces
    constants.ts         # App constants
    utils.ts             # Utility functions (cn for class merging)
  providers/
    auth-provider.tsx    # Auth context (login, logout, token management)
    query-provider.tsx   # React Query provider
  middleware.ts          # Route protection (redirect to /login if no token)
```

The frontend communicates with the backend REST API via `apiClient` which automatically injects the Bearer token from `localStorage` and handles 401 redirects.

### Celery Workers

Background processing uses Celery with Redis as broker and result backend. Four dedicated queues handle different workload types:

| Queue | Purpose |
|---|---|
| `default` | General-purpose tasks |
| `sync` | Data synchronization from payment processors |
| `recon` | Payout reconciliation against orders/payments |
| `export` | Report generation and export |

Tasks extend a `BaseTask` class that provides correlation ID tracking, structured logging, and error handling.

### MCP (Model Context Protocol) Server

The MCP server exposes AI-callable tools with a governance layer:

```
Request --> Rate Limit Check --> Param Validation --> Execute --> Redact --> Audit Log
```

**Governance controls:**
- **Rate limiting**: Per-tenant, per-tool limits (configurable per tool)
- **Parameter validation**: Allowlisted parameters only; unknown params are stripped
- **Default/max limits**: Automatic `LIMIT` enforcement for query tools
- **Result redaction**: Sensitive fields (password, secret, token, api_key, credentials) are masked
- **Audit logging**: Every tool call is logged with correlation ID

Available tools: `netsuite.suiteql`, `recon.run`, `report.export`, `schedule.create`, `schedule.list`, `schedule.run`

The server runs as a standalone process and supports an stdio-based JSON protocol for testing.

### Data Model

The canonical data model normalizes data across payment processors:

```
Orders -----< Payments
  |            |
  +-----< Refunds
  |
  +--- Disputes

Payouts -----< PayoutLines

NetsuitePostings (journal entries posted to NetSuite)
```

All canonical tables share common columns via `CanonicalMixin`:
- `tenant_id` -- tenant scoping
- `dedupe_key` -- unique constraint per tenant for idempotent upserts
- `source` -- origin system (e.g., "stripe", "shopify")
- `source_id` -- ID in the source system
- `subsidiary_id` -- NetSuite subsidiary mapping
- `raw_data` -- original JSON payload

See [DATA_MODEL_OVERVIEW.md](DATA_MODEL_OVERVIEW.md) for full schema details.

### Infrastructure

The entire stack runs via Docker Compose:

| Service | Image | Port |
|---|---|---|
| `postgres` | pgvector/pgvector:pg16 | 5432 |
| `redis` | redis:7-alpine | 6379 |
| `backend` | Custom (Python 3.11-slim) | 8000 |
| `worker` | Same as backend | -- |
| `frontend` | Custom (Node 20-alpine) | 3002 |

PostgreSQL extensions: `uuid-ossp`, `pgcrypto`, `vector` (pgvector).

Database migrations are managed with Alembic.

### Observability

- **Structured logging** via `structlog` with JSON output
- **Correlation IDs** propagated via `X-Correlation-ID` header through the middleware, bound to all log entries
- **Per-request context** binds `tenant_id` and `user_id` to log entries via `structlog.contextvars`

See [OBSERVABILITY_IMPLEMENTATION.md](OBSERVABILITY_IMPLEMENTATION.md) for details.

### Security

- JWT-based authentication with access/refresh token rotation
- bcrypt password hashing
- Fernet symmetric encryption for stored credentials with key versioning
- Row-level security in PostgreSQL
- CORS configuration
- Permission-gated endpoints
- MCP parameter allowlisting and result redaction

See [SECURITY_IMPLEMENTATION_PLAN.md](SECURITY_IMPLEMENTATION_PLAN.md) for the full security plan.
