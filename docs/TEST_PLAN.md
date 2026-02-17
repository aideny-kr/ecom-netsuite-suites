# Test Plan — Phase 2C

## Overview

This document summarizes the full test coverage across backend unit/integration tests, MCP governance/client tests, chat security tests, ingestion tests, and frontend E2E smoke tests. All backend tests run against a real PostgreSQL database with RLS enforcement.

**Current count:** ~190 backend tests passing, 10 frontend E2E tests

---

## Test Pyramid

```
         ┌──────────┐
         │   E2E    │  10 Playwright tests (auth, nav, tables)
         ├──────────┤
         │ Integr.  │  ~80 tests (API endpoints, DB, auth, audit, MCP client)
         ├──────────┤
         │   Unit   │  ~110 tests (services, governance, SuiteQL, ingestion, chat security)
         └──────────┘
```

---

## Backend Test Suite

Run with: `cd backend && python -m pytest tests/ -v --tb=short --cov=app --cov-report=term-missing`

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
| Tool configs (2) | All tools registered, all have required fields (timeout, rate_limit, entitlement, allowlist) |

### 8. MCP Client Contract Tests (`test_mcp_client.py` — 18 tests)

| Area | Tests | What they verify |
|------|-------|-----------------|
| ListTools (3) | Tool list matches expected set (11 tools), all have descriptions, all have input schemas |
| HealthTool (1) | Health tool returns status, timestamp, correct tool count |
| SuiteQL Stub (1) | SuiteQL requires context (tenant_id + db), returns graceful error without it |
| DataSampleTableRead (2) | Valid table returns columns/rows, invalid table returns error with table name |
| UnknownTool (2) | Nonexistent tool rejected, unregistered tool returns "Unknown tool" error |
| RateLimit (1) | Rate limit enforced after N calls per minute |
| ParamFiltering (1) | Evil params stripped, only allowlisted params reach execute |
| CorrelationId (1) | Correlation ID accepted without error |
| Metrics (2) | Tool call metrics recorded, rate limit rejection metrics recorded |
| DisallowedCalls (3) | Disallowed table name rejected, dangerous query params stripped, unregistered tool errors |
| AuditDBWrites (3) | Success audit row written, rate limit denial audited, error audit row written |

### 9. Chat Security (`test_chat_security.py` — 15 tests)

| Area | Tests | What they verify |
|------|-------|-----------------|
| AllowedChatTools (3) | ALLOWED_CHAT_TOOLS is frozenset, contains only read tools, write tools blocked |
| SanitizeUserInput (8) | Strips `</instructions>`, `<system>`, `</prompt>`, `<context>`, `<tool_call>` tags, case insensitive, preserves normal text, strips whitespace |
| IsReadOnlySql (4+) | SELECT allowed, INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE blocked, multi-statement blocked, empty/whitespace blocked |

### 10. SuiteQL Tool Tests (`test_netsuite_suiteql.py` — 24 tests)

| Area | Tests | What they verify |
|------|-------|-----------------|
| ParseTables (6) | Extracts tables from SELECT, JOIN, subquery, mixed case, no tables, alias handling |
| ValidateQuery (9) | Allowed tables pass, disallowed tables blocked, mixed allowed/disallowed blocked, read-only enforced (INSERT/UPDATE/DELETE/DROP rejected), empty query rejected |
| EnforceLimit (7) | LIMIT injected when missing, existing LIMIT preserved when under max, LIMIT capped when over max, FETCH FIRST handled, OFFSET preserved |
| MalformedQueries (2) | Graceful handling of gibberish and SQL injection attempts |

### 11. Ingestion Tests (`test_ingestion.py` — 7 tests)

| Area | Tests | What they verify |
|------|-------|-----------------|
| Stripe Sync (2) | Payouts + payout lines synced from mocked Stripe API, idempotent (second run = same count) |
| Shopify Sync (2) | Orders + refunds + payments synced from mocked Shopify API, idempotent |
| Cursor State (3) | Cursor saved after sync, cursor loaded for incremental sync, cursor updated on re-sync |

### 12. Additional Test Files

| File | Tests | Coverage |
|------|-------|---------|
| `test_chat_api.py` | Chat session CRUD, message send/receive | API layer |
| `test_chat_orchestrator.py` | LangGraph node execution, tool routing | Chat agent |
| `test_netsuite_client.py` | NetSuite API client mocking | HTTP client |
| `test_netsuite_oauth.py` | OAuth flow, token exchange | Auth flow |

---

## Frontend E2E Smoke Tests (Playwright)

Run with: `cd frontend && npx playwright test`

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

Seven jobs triggered on push to `main` and all PRs:

| Job | What it runs | Required |
|-----|-------------|----------|
| `lint` | `ruff check` + `ruff format --check` on backend | Yes |
| `backend-tests` | Alembic migrations + pytest with `--cov-fail-under=60` against PostgreSQL 16 + Redis 7 | Yes |
| `frontend-lint` | `next lint` on frontend | Yes |
| `frontend-typecheck` | `tsc --noEmit` — catches compile errors | Yes |
| `frontend-build` | `npm run build` — verifies production build | Yes |
| `secret-scan` | Gitleaks full-history scan | Yes |
| `e2e-smoke` | Playwright tests (PR-only, currently soft-fail) | No (pending full stack in CI) |
| `required-checks` | Gate job — depends on all required jobs above | Branch protection target |

---

## Coverage Gaps & Next Steps

| Area | Current State | Priority | Next Steps |
|------|--------------|----------|------------|
| **Celery worker integration** | InstrumentedTask tested indirectly | Medium | Add test that triggers sync task and verifies Job + audit events |
| **CSV/Excel export** | Endpoint exists, untested | Medium | Add test for CSV download with correct headers |
| **Frontend E2E in CI** | Playwright configured, CI job placeholder | High | Wire up docker-compose stack in CI for full E2E |
| **Login rate limiting** | Described in SECURITY.md, not tested | High | Add test for 429 after 10 rapid failed logins |
| **Failed login audit** | Not currently audited (SECURITY_VERIFICATION F8) | High | Add audit event + test |
| **Load/stress testing** | None | Low | Consider k6 for rate limit and RLS performance |
| **Refresh token in HttpOnly cookie** | Currently in JSON body (SECURITY_VERIFICATION F3) | High | Move to cookie, add tests |

---

## Related Documents

- [QA_CHECKLIST.md](./QA_CHECKLIST.md) — Definition of Done for every PR
- [SECURITY_VERIFICATION.md](./SECURITY_VERIFICATION.md) — OWASP ASVS-inspired security checklist
- [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) — Deploy gates and rollback procedures
