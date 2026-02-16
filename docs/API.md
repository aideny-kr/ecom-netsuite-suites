# API Reference

Base URL: `http://localhost:8000/api/v1`

All endpoints (except health and auth) require a Bearer token in the `Authorization` header. Tokens are obtained via the `/auth/login` or `/auth/register` endpoints.

Interactive Swagger documentation is available at `http://localhost:8000/docs` when the backend is running.

---

## Health

### `GET /health`

Returns service health status.

**Response** `200 OK`

```json
{
  "status": "ok",
  "database": "ok",
  "redis": "ok"
}
```

`status` is `"ok"` when all dependencies are healthy, `"degraded"` otherwise.

---

## Authentication

### `POST /auth/register`

Register a new tenant and admin user.

**Request Body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `tenant_name` | string | yes | 2-255 chars |
| `tenant_slug` | string | yes | 2-255 chars, lowercase alphanumeric and hyphens |
| `email` | string | yes | valid email |
| `password` | string | yes | 8-128 chars |
| `full_name` | string | yes | 1-255 chars |

**Response** `201 Created`

```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer"
}
```

### `POST /auth/login`

Authenticate an existing user.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `email` | string | yes |
| `password` | string | yes |

**Response** `200 OK` -- same shape as register response.

### `POST /auth/refresh`

Exchange a refresh token for a new token pair.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `refresh_token` | string | yes |

**Response** `200 OK` -- same shape as register response.

### `GET /auth/me`

Get the current user's profile. Requires authentication.

**Response** `200 OK`

```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "tenant_name": "Acme Corp",
  "email": "user@example.com",
  "full_name": "Jane Doe",
  "actor_type": "human",
  "roles": ["admin", "viewer"]
}
```

### `GET /auth/me/tenants`

List all tenants the current user's email belongs to (for multi-tenant users).

**Response** `200 OK`

```json
[
  {
    "id": "uuid",
    "name": "Acme Corp",
    "slug": "acme-corp",
    "plan": "pro"
  }
]
```

### `POST /auth/switch-tenant`

Switch to a different tenant. The user must have an account with the same email in the target tenant.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `tenant_id` | string | yes |

**Response** `200 OK` -- same shape as register response.

---

## Tenants

All tenant endpoints are scoped to the authenticated user's tenant.

### `GET /tenants/me`

Get the current tenant details.

**Response** `200 OK`

```json
{
  "id": "uuid",
  "name": "Acme Corp",
  "slug": "acme-corp",
  "plan": "pro",
  "plan_expires_at": "2025-12-31T00:00:00",
  "is_active": true
}
```

### `PATCH /tenants/me`

Update tenant name. Requires `tenant.manage` permission.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `name` | string | no |

**Response** `200 OK` -- same shape as GET response.

### `GET /tenants/me/config`

Get tenant configuration (NetSuite posting settings, subsidiaries, account mappings).

**Response** `200 OK`

```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "subsidiaries": {},
  "account_mappings": {},
  "posting_mode": "batch",
  "posting_batch_size": 100,
  "posting_attach_evidence": true,
  "netsuite_account_id": "12345"
}
```

### `PATCH /tenants/me/config`

Update tenant configuration. Requires `tenant.manage` permission. Only provided fields are updated.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `subsidiaries` | object | no |
| `account_mappings` | object | no |
| `posting_mode` | string | no |
| `posting_batch_size` | integer | no |
| `posting_attach_evidence` | boolean | no |
| `netsuite_account_id` | string | no |

**Response** `200 OK` -- same shape as GET config response.

---

## Users

All user endpoints require `users.manage` permission and are scoped to the current tenant.

### `GET /users`

List all users in the current tenant.

**Response** `200 OK`

```json
[
  {
    "id": "uuid",
    "tenant_id": "uuid",
    "email": "user@example.com",
    "full_name": "Jane Doe",
    "actor_type": "human",
    "is_active": true,
    "roles": ["admin"]
  }
]
```

### `POST /users`

Create a new user in the current tenant.

**Request Body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `email` | string | yes | valid email |
| `password` | string | yes | 8-128 chars |
| `full_name` | string | yes | 1-255 chars |

**Response** `201 Created` -- same shape as list item.

### `PATCH /users/{user_id}/roles`

Replace a user's role assignments.

**Request Body**

| Field | Type | Required |
|---|---|---|
| `role_names` | string[] | yes |

**Response** `200 OK` -- same shape as list item.

### `DELETE /users/{user_id}`

Deactivate a user (soft delete). Returns `204 No Content`.

---

## Connections

Manage encrypted connections to payment processors and NetSuite.

### `GET /connections`

List all connections for the current tenant. Requires `connections.view` permission.

**Response** `200 OK`

```json
[
  {
    "id": "uuid",
    "tenant_id": "uuid",
    "provider": "stripe",
    "label": "Stripe Production",
    "status": "active",
    "encryption_key_version": 1,
    "metadata_json": null,
    "created_at": "2025-01-15T10:30:00",
    "created_by": "uuid"
  }
]
```

### `POST /connections`

Create a new connection. Requires `connections.manage` permission. Credentials are encrypted at rest using Fernet. Subject to plan-based connection limits.

**Request Body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `provider` | string | yes | `shopify`, `stripe`, or `netsuite` |
| `label` | string | yes | 1-255 chars |
| `credentials` | object | yes | Provider-specific credentials |

**Response** `201 Created` -- same shape as list item.

### `DELETE /connections/{connection_id}`

Delete a connection. Requires `connections.manage` permission. Returns `204 No Content`.

### `POST /connections/{connection_id}/test`

Test a connection's credentials. Requires `connections.manage` permission.

**Response** `200 OK`

```json
{
  "connection_id": "uuid",
  "status": "ok",
  "message": "Connection successful"
}
```

---

## Tables (Canonical Data)

Query and export canonical data tables. Data is tenant-scoped via row-level security.

### Allowed Tables

`orders`, `payments`, `refunds`, `payouts`, `payout_lines`, `disputes`, `netsuite_postings`

### `GET /tables/{table_name}`

Paginated query with optional filtering and sorting. Requires `tables.view` permission.

**Query Parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `page` | int | 1 | Page number (>= 1) |
| `page_size` | int | 50 | Items per page (1-500) |
| `sort_by` | string | null | Column name to sort by |
| `sort_order` | string | `desc` | `asc` or `desc` |
| `status` | string | null | Filter by status |
| `currency` | string | null | Filter by currency |
| `source` | string | null | Filter by source |

**Response** `200 OK`

```json
{
  "items": [ { "id": "uuid", "order_number": "1001", ... } ],
  "total": 250,
  "page": 1,
  "page_size": 50,
  "pages": 5
}
```

### `GET /tables/{table_name}/export/csv`

Export the full table as CSV (up to 10,000 rows). Requires `exports.csv` permission. Returns `text/csv` with a `Content-Disposition` header.

---

## Jobs

View background job status. Requires `tables.view` permission.

### `GET /jobs`

List jobs with pagination.

**Query Parameters**

| Param | Type | Default |
|---|---|---|
| `page` | int | 1 |
| `page_size` | int | 50 |

**Response** `200 OK`

```json
{
  "items": [
    {
      "id": "uuid",
      "tenant_id": "uuid",
      "job_type": "sync",
      "status": "completed",
      "correlation_id": "uuid",
      "connection_id": "uuid",
      "started_at": "2025-01-15T10:30:00",
      "completed_at": "2025-01-15T10:31:00",
      "parameters": {},
      "result_summary": {},
      "error_message": null,
      "celery_task_id": "abc-123"
    }
  ],
  "total": 10,
  "page": 1,
  "page_size": 50,
  "pages": 1
}
```

### `GET /jobs/{job_id}`

Get a single job by ID.

**Response** `200 OK` -- same shape as list item.

---

## Audit Events

View the audit trail. Requires `audit.view` permission.

### `GET /audit-events`

List audit events with filtering and pagination.

**Query Parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `page` | int | 1 | Page number |
| `page_size` | int | 50 | Items per page (1-500) |
| `category` | string | null | Filter by category (e.g., `auth`, `connection`, `user`) |
| `action` | string | null | Filter by action (e.g., `user.login`, `connection.create`) |
| `correlation_id` | string | null | Filter by correlation ID |
| `date_from` | datetime | null | Start of date range |
| `date_to` | datetime | null | End of date range |

**Response** `200 OK`

```json
{
  "items": [
    {
      "id": 1,
      "tenant_id": "uuid",
      "timestamp": "2025-01-15T10:30:00",
      "actor_id": "uuid",
      "actor_type": "human",
      "category": "auth",
      "action": "user.login",
      "resource_type": null,
      "resource_id": null,
      "correlation_id": "uuid",
      "job_id": null,
      "payload": null,
      "status": "success",
      "error_message": null
    }
  ],
  "total": 100,
  "page": 1,
  "page_size": 50,
  "pages": 2
}
```

---

## MCP Tools

The MCP (Model Context Protocol) server exposes AI-callable tools on a separate process (default port 8001). Tools are governed with rate limiting, parameter validation, and result redaction.

### Available Tools

| Tool | Description | Rate Limit |
|---|---|---|
| `netsuite.suiteql` | Execute SuiteQL queries against NetSuite | 30/min |
| `recon.run` | Run a reconciliation between payouts and orders/payments | 10/min |
| `report.export` | Export a report in various formats | 20/min |
| `schedule.create` | Create a scheduled job | 10/min |
| `schedule.list` | List all schedules | 30/min |
| `schedule.run` | Manually trigger a schedule | 10/min |

All MCP tools require the `mcp_tools` entitlement (pro or enterprise plan).

---

## Error Responses

All error responses follow this format:

```json
{
  "detail": "Error description"
}
```

| Status Code | Meaning |
|---|---|
| `400` | Bad request / validation error |
| `401` | Invalid or missing authentication token |
| `403` | Insufficient permissions or plan entitlement |
| `404` | Resource not found |

## Permissions

Endpoints are gated by the following permission codenames:

| Permission | Used By |
|---|---|
| `tenant.manage` | Tenant update, config update |
| `users.manage` | User CRUD, role assignment |
| `connections.view` | List connections |
| `connections.manage` | Create, delete, test connections |
| `tables.view` | Query tables, list jobs |
| `exports.csv` | CSV export |
| `audit.view` | View audit events |
