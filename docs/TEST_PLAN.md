# Test Plan — Phase 1.5 Hardening

## Overview

This document summarizes the test coverage established during Phase 1.5, covering backend unit/integration tests, MCP governance tests, and frontend E2E smoke tests. All backend tests run against a real PostgreSQL database with RLS enforcement.

---

## Backend Test Suite (96 tests)

Run with: `make test` or `cd backend && python -m pytest tests/ -v --tb=short`

### Test Infrastructure (`tests/conftest.py`)

- Per-test async engine + transaction rollback for full isolation
- Factory functions: `create_test_tenant()`, `create_test_user()`, `make_auth_headers()`
- Pre-built fixtures: `tenant_a`, `tenant_b`, `admin_user`, `readonly_user`, `finance_user`, `admin_user_b`
- FastAPI dependency override injects test DB session with RLS context

### 1. Auth Flows (`test_auth.py` — 16 tests)

| Test | What it verifies |
|------|-----------------|
| Register success | Creates tenant + user, returns JWT tokens |
| Duplicate slug rejected | 409 on conflicting tenant slug |
| Register emits audit event | `audit_events` contains `user.register` entry |
| Invalid slug rejected | 422 on malformed slug |
| Short password rejected | 422 when password < 8 chars |
| Login success | Returns access + refresh tokens |
| Login wrong password | 401 on incorrect credentials |
| Login nonexistent email | 401 for unknown email |
| Login emits audit event | `audit_events` contains `user.login` entry |
| Refresh success | New access token from valid refresh token |
| Refresh invalid token | 401 on garbage refresh token |
| /me returns profile | Authenticated user gets their own profile |
| /me no auth | 401/403 without token |
| List tenants | `/auth/me/tenants` returns all tenant memberships |
| Switch tenant success | New JWT scoped to target tenant |
| Switch tenant no account | 403 when user has no account in target tenant |

### 2. RBAC Permission Enforcement (`test_rbac.py` — 27 tests)

| Role | Allowed | Denied |
|------|---------|--------|
| **Admin** (8 tests) | list/create connections, list/create users, get tenant, get config, view tables, view audit | — |
| **Readonly** (8 tests) | view tables, list connections, view audit | create connection, manage users, update tenant, update config |
| **Finance** (6 tests) | view connections, view tables, view audit | create connection, manage users, update config |
| **Unauthenticated** (5 tests) | health endpoint only | connections, tables, users, audit all return 401/403 |

### 3. Multi-Tenant Isolation (`test_isolation.py` — 7 tests)

| Test | What it verifies |
|------|-----------------|
| Connections isolated | Tenant A cannot see Tenant B's connections |
| Users isolated | Tenant A cannot see Tenant B's users |
| Tables isolated | Orders created by Tenant A invisible to Tenant B |
| Config isolated | Each tenant sees only their own config |
| Audit isolated | Audit events scoped per tenant |
| Cross-tenant delete blocked | Deleting another tenant's connection returns 404 |
| Cross-tenant user delete blocked | Deactivating another tenant's user returns 404 |

### 4. Audit Events & Correlation IDs (`test_audit.py` — 12 tests)

| Test | What it verifies |
|------|-----------------|
| Login audit event | `user.login` event created with correct actor_id |
| Connection create audit | `connection.create` event logged |
| Connection delete audit | `connection.delete` event logged |
| User create audit | `user.create` event logged |
| User deactivate audit | `user.deactivate` event logged |
| Tenant ID present | Every audit event has `tenant_id` set |
| Correlation ID in response | `X-Correlation-ID` header returned |
| Correlation ID propagated | Client-supplied correlation ID flows through |
| Unique per request | Each request gets a distinct correlation ID |
| Filter by category | `/audit-events?category=auth` returns matching events |
| Filter by action | `/audit-events?action=user.login` filters correctly |
| Pagination | `limit` and `offset` params work |

### 5. Connection CRUD & Encryption (`test_connections.py` — 8 tests)

| Test | What it verifies |
|------|-----------------|
| Create connection | 201 with correct provider/label |
| Invalid provider | 422 for unsupported provider |
| List connections | Credentials never exposed in list response |
| Delete connection | 204 on successful delete |
| Delete nonexistent | 404 for missing connection |
| Test connection stub | Returns stub response |
| Credentials encrypted | Raw DB value differs from plaintext |
| Key version stored | `encryption_key_version` matches config |

### 6. Plan Entitlements (`test_entitlements.py` — 8 tests)

| Test | What it verifies |
|------|-----------------|
| Trial create up to limit | 2 connections allowed on trial |
| Trial blocked beyond limit | 3rd connection returns 403 with plan/limit message |
| Pro higher limit | 3+ connections succeed on pro plan |
| Trial connections allowed (service) | `check_entitlement()` returns True when under limit |
| MCP denied on trial | `mcp_tools` entitlement blocked for trial |
| MCP allowed on pro | `mcp_tools` entitlement granted for pro |
| Get plan limits | Returns correct `max_connections` and `mcp_tools` values |
| Inactive tenant denied | Deactivated tenant blocked from all entitlements |

### 7. MCP Tool Governance (`test_mcp.py` — 18 tests)

| Area | Tests | What they verify |
|------|-------|-----------------|
| Param validation (4) | Allowlist filtering, default limit injection, max limit capping, passthrough when no allowlist |
| Rate limiting (3) | Within-limit allowed, over-limit blocked, per-tenant isolation |
| Result redaction (3) | Sensitive keys (token, api_key, password) redacted, nested redaction, safe data unchanged |
| Audit payload (3) | Correct payload structure, sensitive param scrubbing, error payload format |
| Governed execute (3) | Successful execution, rate-limited rejection, error handling |
| Tool configs (2) | All 6 tools registered, all have required fields (timeout, rate_limit, entitlement, allowlist) |

---

## Frontend E2E Smoke Tests (Playwright)

Run with: `make e2e` or `cd frontend && npx playwright test`

### Configuration
- **Browser:** Chromium only
- **Base URL:** `http://localhost:3002` (configurable via `BASE_URL` env)
- **Test directory:** `frontend/e2e/`

### Test Suites

| Suite | Tests | Coverage |
|-------|-------|----------|
| `auth.spec.ts` | 3 | Register new tenant, login after register, wrong password error |
| `navigation.spec.ts` | 5 | Sidebar company name, Dashboard/Connections/Tables/Audit navigation |
| `tables.spec.ts` | 2 | Empty state rendering, table header columns visible |

---

## CI Pipeline (`.github/workflows/ci.yml`)

Three parallel jobs triggered on push to `main` and all PRs:

| Job | What it runs |
|-----|-------------|
| `lint` | `ruff check` + `ruff format --check` on backend |
| `backend-tests` | Alembic migrations + pytest (96 tests) against PostgreSQL 16 + Redis 7 |
| `frontend-lint` | `next lint` on frontend |

---

## Coverage Gaps & Phase 2 Test Priorities

| Area | Current State | Next Steps |
|------|--------------|------------|
| **Data pipeline idempotency** | No tests yet (no live integrations) | Add dedupe_key conflict tests when sync tasks land |
| **Celery worker instrumentation** | InstrumentedTask exists but no integration test | Add test that triggers example_sync and verifies Job + audit events |
| **CSV/Excel export** | Endpoint exists, untested | Add test for CSV download with correct headers |
| **MCP server end-to-end** | Governance unit-tested; no live MCP protocol test | Add integration test via MCP client library |
| **Frontend E2E in CI** | Playwright configured but not in CI workflow | Add CI job with `webServer` config to start backend + frontend |
| **Load/stress testing** | None | Consider k6 or locust for rate limit and RLS performance |
| **Security scanning** | None | Add `bandit` (Python) and `npm audit` to CI |
