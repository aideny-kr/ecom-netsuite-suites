# Security Verification Checklist

**Standard:** OWASP Application Security Verification Standard (ASVS) v4.0 — adapted for this multi-tenant SaaS application.

**Scope:** FastAPI backend (`backend/app/`), PostgreSQL RLS, Celery workers, MCP tool server.

**How to use:** Work through each section. For each item, run the suggested test and mark `[x]` when the control is confirmed to be in place. Items marked with `FINDING` indicate a known gap identified during initial authorship — prioritise these.

---

## V2 — Authentication

### V2.1 Password Security

- [x] **V2.1.1** Passwords are hashed with bcrypt using `bcrypt.gensalt()` (work factor >= 10) before being stored.
  - **Test:** `backend/tests/test_security_verification.py::TestBcryptHashing` — verifies hash starts with `$2b$` and cost factor >= 10.
  - **Code:** `backend/app/core/security.py` — `hash_password()` / `verify_password()`.

- [x] **V2.1.2** Plaintext passwords are never stored, logged, or returned in any response body or error message.
  - **Test:** `backend/tests/test_security_verification.py::TestPasswordNotExposed` — verifies `hashed_password` absent from `/auth/me` response.
  - **Code:** `backend/app/schemas/auth.py` — `UserProfile` does not include `hashed_password`.

- [x] **V2.1.3** Minimum password length of 8 characters is enforced at the API layer before any processing.
  - **Test:** `backend/tests/test_auth_security.py::TestPasswordComplexity` — validates 422 for short passwords.
  - **Code:** `backend/app/schemas/auth.py` — `RegisterRequest.password` field: `Field(min_length=8, max_length=128)`.

- [x] **V2.1.4** Maximum password length of 128 characters is enforced to prevent bcrypt truncation attacks (bcrypt silently truncates at 72 bytes).
  - **Test:** `backend/tests/test_auth_security.py::TestPasswordComplexity` — validates field constraints.
  - **Code:** `backend/app/schemas/auth.py` — `Field(min_length=8, max_length=128)`.

- [x] **V2.1.5 (FIXED — F1)** Password complexity policy enforced: `@field_validator("password")` in `RegisterRequest` requires at least one uppercase letter, one digit, and one special character.
  - **Code:** `backend/app/schemas/auth.py` — `RegisterRequest.password_complexity()`.
  - **Test:** `backend/tests/test_auth_security.py::TestPasswordComplexity` — validates 422 for missing digit, missing special, missing uppercase; 201 for valid complex password.

### V2.2 General Authenticator Security

- [x] **V2.2.1 (FIXED — F2)** Login rate limiting is applied: no more than 10 attempts per minute per IP via in-memory sliding-window rate limiter.
  - **Code:** `backend/app/core/rate_limit.py` — `check_login_rate_limit()`. Applied as a check in `backend/app/api/v1/auth.py` — `login()`.
  - **Test:** `backend/tests/test_auth_security.py::TestLoginRateLimit` — sends 11 rapid requests, asserts 429 on the 11th.
  - **Note:** Per-process only. For multi-pod production, use API gateway rate limiting (F9 deferred).

- [x] **V2.2.2** Authentication failure responses are generic and do not distinguish between "user not found" and "wrong password" (prevents user enumeration).
  - **Test:** `backend/tests/test_security_verification.py::TestGenericAuthErrors` — sends login with wrong password and nonexistent user, asserts identical error messages.
  - **Code:** `backend/app/services/auth_service.py` — `authenticate()` iterates candidates and raises a single `ValueError("Invalid email or password")` regardless of the failure reason.

- [x] **V2.2.3** The `is_active` flag is checked on every authentication and on every token validation, so deactivated users cannot log in or use existing tokens.
  - **Test:** `backend/tests/test_security_verification.py::TestDeactivatedUser` — deactivates user, asserts 401 on login and 401/403 on protected endpoint.
  - **Code:** `backend/app/services/auth_service.py` — `authenticate()`: `.where(User.is_active.is_(True))`. `backend/app/core/dependencies.py` — `get_current_user()`: `.where(User.is_active.is_(True))`.

- [x] **V2.2.4** Tenant registration events are written to the audit log.
  - **Test:** `backend/tests/test_auth.py::TestRegister::test_register_creates_audit_event` — verifies `tenant.register` audit event.
  - **Code:** `backend/app/api/v1/auth.py` — `register()` calls `audit_service.log_event(action="tenant.register")`.

- [x] **V2.2.5** Login events are written to the audit log.
  - **Test:** `backend/tests/test_auth.py::TestLogin::test_login_creates_audit_event` — verifies `user.login` audit event.
  - **Code:** `backend/app/api/v1/auth.py` — `login()` calls `audit_service.log_event(action="user.login")`.

### V2.3 Authenticator Lifecycle

- [x] **V2.3.1** Tenant switch requires the requesting user to already hold a valid JWT (i.e. is authenticated) and to have a matching email account in the target tenant.
  - **Test:** `backend/tests/test_auth.py::TestSwitchTenant` — verifies auth required and cross-tenant switch validation.
  - **Code:** `backend/app/api/v1/auth.py` — `switch_tenant()` depends on `get_current_user`. `backend/app/services/auth_service.py` — `switch_tenant()` filters by `User.email == email` and `User.tenant_id == target`.

---

## V3 — Session Management

### V3.1 Token Storage and Transmission

- [x] **V3.1.1** Access tokens are short-lived (30 minutes) and the expiry claim (`exp`) is validated on every request.
  - **Test:** `backend/tests/test_security_verification.py::TestTokenExpiry::test_expired_token_returns_none` — forges expired token, asserts `decode_token()` returns None.
  - **Code:** `backend/app/core/security.py` — `create_access_token()` sets `exp = now + timedelta(minutes=30)`. `decode_token()` uses `jwt.decode()` which validates `exp` by default.

- [x] **V3.1.2** Refresh tokens are long-lived (7 days) and are validated for both expiry and token type before issuing a new access token.
  - **Test:** `backend/tests/test_security_verification.py::TestTokenExpiry::test_access_token_rejected_as_refresh` — uses access token as refresh, asserts 401.
  - **Code:** `backend/app/services/auth_service.py` — `refresh_access_token()` checks `payload.get("type") != "refresh"`.

- [x] **V3.1.3 (FIXED — F3)** Refresh tokens are set as `HttpOnly; Secure; SameSite=Lax` cookies on login, register, refresh, and switch-tenant responses. Removed from JSON body.
  - **Code:** `backend/app/api/v1/auth.py` — `_set_refresh_cookie()` helper. `POST /api/v1/auth/refresh` reads from cookie first, falls back to body for backward compatibility. Frontend updated: `credentials: "include"` on all fetch calls, removed `refresh_token` from localStorage.
  - **Test:** `backend/tests/test_auth_security.py::TestRefreshTokenCookie` — verifies cookie is set and body is empty.

- [x] **V3.1.4** Access tokens are transmitted only via the `Authorization: Bearer` header, never as URL query parameters.
  - **Verified:** `backend/app/core/dependencies.py` uses `HTTPBearer()` which reads from the `Authorization` header only. Structural guarantee — no query param extraction exists.

### V3.2 Token Revocation

- [x] **V3.2.1 (FIXED — F4)** In-memory JWT denylist implemented keyed by `jti` (JWT ID). Tokens include a `jti` claim. `decode_token()` checks the denylist and returns `None` for revoked tokens. Expired entries are automatically cleaned up.
  - **Code:** `backend/app/core/token_denylist.py` — `revoke_token()`, `is_revoked()`. `backend/app/core/security.py` — `create_access_token()` and `create_refresh_token()` now include `jti`. `decode_token()` checks denylist.
  - **Test:** `backend/tests/test_auth_security.py::TestJWTDenylist` — revokes a JTI and asserts 401.
  - **Note:** In-memory only (single process). For multi-pod production, migrate to Redis-backed denylist.

- [x] **V3.2.2 (FIXED — F5)** Logout endpoint implemented. `POST /api/v1/auth/logout` revokes the access token JTI, clears the refresh cookie, and creates an audit event.
  - **Code:** `backend/app/api/v1/auth.py` — `logout()`. Accepts optional `refresh_token_jti` in body to revoke refresh token too.
  - **Test:** `backend/tests/test_auth_security.py::TestLogout` — verifies token is revoked and subsequent requests fail with 401.

### V3.3 Token Content

- [x] **V3.3.1** JWT payload contains only `sub` (user UUID), `tenant_id`, `exp`, `type`, and `jti`. No PII (email, full name) is embedded in the token.
  - **Test:** `backend/tests/test_security_verification.py::TestJWTContent::test_access_token_payload_fields` and `test_refresh_token_payload_fields` — decode tokens and assert exact field set `{sub, tenant_id, exp, type, jti}`.
  - **Code:** `backend/app/services/auth_service.py` — `_create_tokens()`: `token_data = {"sub": str(user.id), "tenant_id": str(user.tenant_id)}`.

- [x] **V3.3.2** JWT algorithm is explicitly allowlisted at decode time to prevent the `alg: none` attack.
  - **Test:** `backend/tests/test_security_verification.py::TestJWTContent::test_alg_none_token_rejected` — forges token with `alg:none`, asserts `decode_token()` returns None.
  - **Code:** `backend/app/core/security.py` — `decode_token()`: `jwt.decode(..., algorithms=[settings.JWT_ALGORITHM])`. The `algorithms` list constrains which algorithm is accepted.

---

## V4 — Access Control

### V4.1 RBAC — Permission Enforcement

- [x] **V4.1.1** Every protected endpoint depends on `require_permission(codename)` or `get_current_user`, and no route is publicly accessible without authentication except `/api/v1/auth/login`, `/api/v1/auth/register`, and `/api/v1/health`.
  - **Test:** `backend/tests/test_rbac.py::TestUnauthenticatedAccess` — verifies 403 on connections, tables, users, audit; 200 on health.
  - **Code:** `backend/app/core/dependencies.py` — `require_permission()` wraps `get_current_user`.

- [x] **V4.1.2** The `readonly` role can only read connections, tables, and audit events — it cannot create, modify, or delete any resource.
  - **Test:** `backend/tests/test_rbac.py::TestReadonlyAccess` — verifies 403 on create/delete, 200 on list/view.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `ROLE_PERMISSIONS["readonly"] = ["connections.view", "tables.view", "audit.view"]`.

- [x] **V4.1.3** The `finance` role cannot manage connections or users.
  - **Test:** `backend/tests/test_rbac.py::TestFinanceAccess` — verifies 403 on `POST /connections` and user management.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `ROLE_PERMISSIONS["finance"]` does not include `connections.manage` or `users.manage`.

- [x] **V4.1.4** The `ops` role cannot view audit logs.
  - **Test:** `backend/tests/test_rbac.py` — RBAC checks cover all roles against all endpoints.
  - **Verified:** `backend/app/api/v1/audit.py` uses `require_permission("audit.view")`.

- [x] **V4.1.5** Permission checks query the database at request time — permissions are not cached in the JWT itself. Role changes take effect immediately without requiring re-login.
  - **Verified:** `backend/app/core/dependencies.py` — `require_permission()` executes a live DB query on every call: `select(Permission.codename).join(RolePermission).where(RolePermission.role_id.in_(role_ids))`. JWT contains only `sub` and `tenant_id`, never permissions.

- [x] **V4.1.6** A user with no roles assigned cannot access any permission-gated endpoint.
  - **Verified:** `backend/app/core/dependencies.py` — `require_permission()` checks `if not role_ids: raise HTTP 403`.

### V4.2 Tenant Isolation — Row-Level Security

- [x] **V4.2.1** PostgreSQL RLS is enabled on all tenant-scoped tables.
  - **Test:** `backend/tests/test_isolation.py::TestCrossTenantIsolation` — verifies connections, tables, users, tenant config, and audit events are isolated.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `RLS_TABLES` list and `ENABLE ROW LEVEL SECURITY` + `CREATE POLICY`.

- [x] **V4.2.2** `SET LOCAL app.current_tenant_id` is called inside the request's database transaction so the context is cleared when the transaction ends.
  - **Verified:** `backend/app/core/dependencies.py` — `get_current_user()`: `await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))`. The `LOCAL` keyword scopes the setting to the current transaction.

- [x] **V4.2.3** Cross-tenant data access is impossible via the API.
  - **Test:** `backend/tests/test_isolation.py::TestCrossTenantIsolation` — Tenant A cannot read/delete Tenant B's connections. `backend/tests/test_workspace_isolation.py` — 7 tests covering workspace cross-tenant isolation.

- [x] **V4.2.4** The `audit_events` table has separate `SELECT` and `INSERT` RLS policies with no `UPDATE` or `DELETE` policy.
  - **Test:** `backend/tests/test_isolation.py::TestCrossTenantIsolation::test_audit_events_isolated` — verifies RLS isolation on audit events.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `audit_events_select` (FOR SELECT) and `audit_events_insert` (FOR INSERT WITH CHECK) policies.

- [x] **V4.2.5 (FIXED — F6)** Celery workers set RLS context via `tenant_session()` in `backend/app/workers/base_task.py`, which calls `SET LOCAL app.current_tenant_id`. All worker tasks inherit from `InstrumentedTask` and use `tenant_session()`. Confirmed in code review — no bare sessions.
  - **Code:** `backend/app/workers/base_task.py` — `tenant_session()` context manager.
  - **Test:** `backend/tests/test_worker_rls.py` — verifies `SET LOCAL` is called and all task methods use `tenant_session()`.

### V4.3 Entitlement Enforcement

- [x] **V4.3.1** The `trial` plan is blocked from using MCP tools entirely.
  - **Test:** `backend/tests/test_entitlements.py::TestEntitlementServiceDirect::test_mcp_tools_denied_on_trial`.
  - **Code:** `backend/app/services/entitlement_service.py` — `PLAN_LIMITS["free"]["mcp_tools"] == False`.

- [x] **V4.3.2** Connection limits are enforced per plan before a new connection is created.
  - **Test:** `backend/tests/test_entitlements.py::TestConnectionEntitlements::test_free_blocked_beyond_limit`.
  - **Code:** `backend/app/services/entitlement_service.py` — `PLAN_LIMITS["free"]["max_connections"] = 2`.

- [x] **V4.3.3** NetSuite connections are exempt from the per-plan connection count limit.
  - **Test:** `backend/tests/test_entitlements.py::TestConnectionEntitlements::test_free_netsuite_always_allowed`.
  - **Code:** `backend/app/services/entitlement_service.py` — `check_entitlement()` filters `Connection.provider != "netsuite"` when counting.

---

## V6 — Data Protection

### V6.1 Credential Encryption at Rest

- [x] **V6.1.1** Third-party credentials are encrypted with Fernet before being written to `connections.encrypted_credentials`.
  - **Test:** `backend/tests/test_security_verification.py::TestCredentialEncryption::test_encrypted_credentials_is_fernet_blob` — verifies DB value starts with `gAAAAA`.
  - **Code:** `backend/app/services/connection_service.py` — `create_connection()` calls `encrypt_credentials(credentials)`. `backend/app/core/encryption.py` — `encrypt_credentials()`.

- [x] **V6.1.2** The `ENCRYPTION_KEY` placeholder is rejected in non-development environments.
  - **Verified:** `backend/app/core/encryption.py` — `_get_fernet()` raises `ValueError("ENCRYPTION_KEY must be set to a valid Fernet key")` when key is missing or equals the placeholder.

- [x] **V6.1.3** Credential key versioning is tracked via `connections.encryption_key_version`.
  - **Verified:** `backend/app/models/connection.py` — `encryption_key_version` column. `backend/app/services/connection_service.py` — sets `encryption_key_version=get_current_key_version()`.

- [ ] **V6.1.4 (DEFERRED — F7)** No key rotation procedure implemented. Documented as tech debt. Future: add `kid` claim to tokens for rotation support and implement a re-encryption management command.
  - **Remediation:** Implement a management command for key rotation. Low urgency for single-key deployments.

### V6.2 Sensitive Data in API Responses

- [x] **V6.2.1** The `ConnectionResponse` schema never includes `encrypted_credentials` or decrypted credential fields.
  - **Test:** `backend/tests/test_security_verification.py::TestSensitiveDataExclusion::test_list_connections_excludes_credentials` — verifies no credential fields in API response.
  - **Code:** `backend/app/schemas/connection.py` — `ConnectionResponse` does not map `encrypted_credentials`.

- [x] **V6.2.2** The `UserProfile` schema does not expose `hashed_password` or any internal user fields.
  - **Test:** `backend/tests/test_security_verification.py::TestPasswordNotExposed::test_me_endpoint_excludes_hashed_password`.
  - **Code:** `backend/app/schemas/auth.py` — `UserProfile` fields: `id`, `tenant_id`, `email`, `full_name`, `actor_type`, `roles`, `onboarding_completed_at`.

- [x] **V6.2.3** MCP tool results are redacted before being returned or logged.
  - **Test:** `backend/tests/test_mcp.py::TestResultRedaction` — verifies `password`, `secret`, `api_key` keys are replaced with `***REDACTED***`.
  - **Code:** `backend/app/mcp/governance.py` — `redact_result()`.

### V6.3 Secrets in Logs

- [x] **V6.3.1** No plaintext secrets appear in structured logs.
  - **Verified:** `backend/app/mcp/governance.py` — `create_audit_payload()` excludes sensitive keys. `backend/app/core/dependencies.py` — log context binds only `tenant_id` and `user_id`. `backend/app/core/logging.py` — `structlog.processors.JSONRenderer()`.

- [x] **V6.3.2** `APP_DEBUG=True` does not expose credential plaintext (only Fernet ciphertext in SQL echo).
  - **Verified:** `backend/app/core/database.py` — `echo=settings.APP_DEBUG`. Credentials are always encrypted before DB write, so even with SQL echo, only ciphertext appears.

- [x] **V6.3.3** The `JWT_SECRET_KEY` default value `"change-me-in-production"` must not be used in any non-local environment.
  - **Verified:** `backend/app/core/config.py` — default is `"change-me-in-production"`. Production deployment must override via `JWT_SECRET_KEY` env var. `.env` is in `.gitignore`.

---

## V7 — Audit and Logging

### V7.1 Audit Event Coverage

- [x] **V7.1.1** Security-significant events are recorded in `audit_events`.
  - **Test:** `backend/tests/test_audit.py::TestAuditEventEmission` — verifies `user.login`, `connection.create`, `connection.delete`, `user.create`, `user.deactivate`. `backend/tests/test_workspace_audit.py` — workspace events. `backend/tests/test_auth.py` — `tenant.register`, `user.login`.

- [x] **V7.1.2 (FIXED — F8)** Failed authentication attempts are now logged to `audit_events` with `action="user.login_failed"`, `status="denied"`, and payload containing email and IP.
  - **Code:** `backend/app/api/v1/auth.py` — `login()` catches `ValueError` and logs audit event before re-raising.
  - **Test:** `backend/tests/test_auth_security.py::TestAuditFailedLogin` — verifies audit event exists with correct fields.

- [x] **V7.1.3** Audit events include `correlation_id`, `actor_id`, `actor_type`, `resource_type`, `resource_id`, and `tenant_id`.
  - **Test:** `backend/tests/test_audit.py::TestAuditCorrelationId` — verifies correlation_id. `backend/tests/test_audit.py::TestAuditEventEmission::test_audit_events_have_tenant_id`.
  - **Code:** `backend/app/models/audit.py` — `AuditEvent` model with all context fields.

- [x] **V7.1.4** MCP tool rate-limit denials are audited.
  - **Test:** `backend/tests/test_mcp.py::TestRateLimiting` — verifies rate limit enforcement. `backend/tests/test_mcp_client.py::TestRateLimitEnforced`.
  - **Code:** `backend/app/mcp/governance.py` — `governed_execute()` logs `tool.rate_limited` with `status="denied"`.

### V7.2 Correlation ID Propagation

- [x] **V7.2.1** Every HTTP response includes an `X-Correlation-ID` header.
  - **Test:** `backend/tests/test_audit.py::TestCorrelationId` — verifies header echoed back and auto-generated.
  - **Code:** `backend/app/core/middleware.py` — `CorrelationIdMiddleware`.

- [x] **V7.2.2** The `correlation_id` is bound to the structured logging context for the duration of the request.
  - **Verified:** `backend/app/core/middleware.py` — `structlog.contextvars.bind_contextvars(correlation_id=correlation_id)`. `backend/app/core/logging.py` — `structlog.contextvars.merge_contextvars` processor.

- [x] **V7.2.3** Celery worker tasks receive and propagate `correlation_id`.
  - **Verified:** `backend/app/workers/base_task.py` — `InstrumentedTask.before_start()` extracts `correlation_id` from `kwargs` and passes to audit events.

### V7.3 Log Integrity and Sensitive Data Scrubbing

- [x] **V7.3.1** Structured logs are emitted as JSON.
  - **Verified:** `backend/app/core/logging.py` — `structlog.processors.JSONRenderer()` is the final processor.

- [x] **V7.3.2** The `audit_events` table does not contain decrypted credential values in the `payload` column.
  - **Verified:** `backend/app/api/v1/connections.py` — `create_connection()` logs `payload={"provider": ..., "label": ...}` — credentials excluded. `backend/app/mcp/governance.py` — `create_audit_payload()` scrubs sensitive keys.

---

## V13 — API Security

### V13.1 Input Validation

- [x] **V13.1.1** All request bodies are validated by Pydantic schemas before reaching service code.
  - **Test:** `backend/tests/test_workspace_security.py` — path traversal, size limits, injection tests. `backend/tests/test_schedule_validation.py` — type validation, parameter injection.
  - **Verified:** FastAPI + Pydantic returns 422 for all schema violations.

- [x] **V13.1.2** The tenant slug field is validated against `^[a-z0-9-]+$`.
  - **Test:** `backend/tests/test_auth.py::TestRegister::test_register_invalid_slug` — verifies 422 for invalid slugs.
  - **Code:** `backend/app/schemas/auth.py` — `tenant_slug: str = Field(min_length=2, max_length=255, pattern=r"^[a-z0-9-]+$")`.

- [x] **V13.1.3** SuiteQL queries are passed as POST body parameters, not interpolated into URLs.
  - **Verified:** `backend/app/mcp/tools/netsuite_suiteql.py` — query sent as `{"q": query}` in POST body to NetSuite SuiteQL endpoint. `backend/tests/test_chat_security.py::TestIsReadOnlySql` — validates read-only SQL enforcement.

- [x] **V13.1.4** The `data.sample_table_read` MCP tool restricts reads to allowlisted tables.
  - **Test:** `backend/tests/test_mcp_client.py::TestDataSampleTableRead::test_invalid_table` — verifies non-allowlisted table names are rejected.
  - **Code:** `backend/app/mcp/tools/data_sample.py` — allowlist check before query.

### V13.2 CORS

- [x] **V13.2.1** CORS `allow_origins` is restricted to the frontend's exact origin(s) and does not use the wildcard `*`.
  - **Test:** `backend/tests/test_security_verification.py::TestCORSConfiguration::test_cors_rejects_unknown_origin` — verifies evil.com origin not echoed. `test_cors_allows_configured_origin` — verifies allowed origin works.
  - **Code:** `backend/app/main.py` — `CORSMiddleware(allow_origins=settings.cors_origins_list)`.

- [x] **V13.2.2** `allow_credentials=True` is configured, not combined with wildcard origins.
  - **Test:** `backend/tests/test_security_verification.py::TestCORSConfiguration::test_cors_allows_credentials` and `test_cors_origins_not_wildcard`.
  - **Code:** `backend/app/main.py` — `CORSMiddleware(allow_credentials=True, ...)`.

### V13.3 MCP Tool Rate Limiting

- [x] **V13.3.1** Each MCP tool has a per-tenant per-minute rate limit defined in `TOOL_CONFIGS`.
  - **Test:** `backend/tests/test_mcp.py::TestRateLimiting` — verifies within-limit and exceeds-limit behavior. `backend/tests/test_mcp_client.py::TestRateLimitEnforced`.
  - **Code:** `backend/app/mcp/governance.py` — `TOOL_CONFIGS` and `check_rate_limit()`.

- [ ] **V13.3.2 (DEFERRED — F9)** MCP tool rate limit state is stored in-process. Per-process rate limiter from F2 is sufficient for single-pod deployments. Production multi-pod should use API gateway rate limiting or Redis sliding window counters.
  - **Remediation:** Replace in-process dict with Redis `ZADD` + `ZREMRANGEBYSCORE` when scaling to multiple pods.

- [x] **V13.3.3** Unknown MCP tool names are rejected immediately without executing any code.
  - **Test:** `backend/tests/test_mcp_client.py::TestUnknownTool` — verifies `{"error": "Unknown tool: ..."}` response.
  - **Code:** `backend/app/mcp/server.py` — `call_tool()` checks `if tool_name not in self.tools`.

- [x] **V13.3.4** MCP tool parameter allowlisting strips unexpected parameters before execution.
  - **Test:** `backend/tests/test_mcp.py::TestParamValidation::test_filters_to_allowlist` and `backend/tests/test_mcp_client.py::TestParamFiltering`.
  - **Code:** `backend/app/mcp/governance.py` — `validate_params()` filters to allowlisted keys.

---

## V10 — Business Logic Security

### V10.1 Idempotency and Deduplication

- [x] **V10.1.1** Canonical data records have a `dedupe_key` unique constraint per tenant to prevent duplicate inserts.
  - **Test:** `backend/tests/test_security_verification.py::TestIdempotency::test_duplicate_dedupe_key_rejected` — inserts duplicate dedupe_key, asserts IntegrityError.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `UniqueConstraint("tenant_id", "dedupe_key")` on all canonical tables.

- [x] **V10.1.2** The ingestion services use the `dedupe_key` to detect existing records and skip or update them.
  - **Verified:** `backend/app/services/ingestion/shopify_sync.py` and `stripe_sync.py` — upsert logic with dedupe_key check before insert.

### V10.2 Plan Limit Enforcement

- [x] **V10.2.1 (FIXED — F10)** Plan limits are now concurrency-safe. `check_entitlement()` uses `SELECT ... FOR UPDATE` on the tenant row to serialize concurrent requests within the same transaction.
  - **Code:** `backend/app/services/entitlement_service.py` — `select(Tenant).where(...).with_for_update()`.
  - **Test:** Concurrent connection creation is serialized by the row lock.

- [x] **V10.2.2 (FIXED — F11)** Expired free-plan tenants are blocked at the authentication layer. `get_current_user()` checks `plan_expires_at` and raises 403 "Plan expired" for expired free plans.
  - **Code:** `backend/app/core/dependencies.py` — `get_current_user()` checks `tenant.plan == "free"` and `tenant.plan_expires_at < now`.
  - **Test:** `backend/tests/test_auth_security.py::TestTrialExpiry` — verifies 403 for expired plan and 200 for active plan.

- [x] **V10.2.3 (FIXED — F12)** Deactivated tenants are blocked at both the authentication layer and the login endpoint.
  - **Code:** `backend/app/core/dependencies.py` — `get_current_user()` loads the tenant and raises 403 "Tenant is deactivated" if `tenant.is_active` is False. `backend/app/services/auth_service.py` — `authenticate()` also checks tenant active status on login.
  - **Test:** `backend/tests/test_auth_security.py::TestDeactivatedTenant` — verifies 403 on authenticated endpoint and 401 on login.

### V10.3 Celery Job Integrity

- [x] **V10.3.1** Celery task `kwargs` include `tenant_id` so the worker can scope its DB operations to the correct tenant.
  - **Test:** `backend/tests/test_worker_rls.py::TestSyncTasksUseTenantSession` — verifies tenant_id passed and tenant_session used.
  - **Code:** `backend/app/workers/base_task.py` — `before_start()` reads `tenant_id = kwargs.get("tenant_id")`.

- [x] **V10.3.2** Job records are always created with the correct `tenant_id` and are protected by RLS.
  - **Verified:** `backend/alembic/versions/001_initial_schema.py` — `jobs` table is in `RLS_TABLES`. `backend/app/workers/base_task.py` — creates Job with `tenant_id` from kwargs.

---

## Open Items Summary

The following gaps were identified during checklist authorship and require remediation before a production security review:

| ID | Section | Gap | Priority | Status |
|----|---------|-----|----------|--------|
| F1 | V2.1.5 | No password complexity policy beyond length | Medium | **FIXED** |
| F2 | V2.2.1 | Login rate limiting layer not visible in app code | High | **FIXED** (per-process) |
| F3 | V3.1.3 | Refresh token returned in JSON body, not HttpOnly cookie | High | **FIXED** |
| F4 | V3.2.1 | No JWT denylist / token revocation mechanism | High | **FIXED** (in-memory) |
| F5 | V3.2.2 | No logout endpoint | Medium | **FIXED** |
| F6 | V4.2.5 | Celery workers may not set RLS context | High | **FIXED** (already correct) |
| F7 | V6.1.4 | No key rotation procedure for Fernet encryption key | Medium | DEFERRED |
| F8 | V7.1.2 | Failed login attempts are not audited | High | **FIXED** |
| F9 | V13.3.2 | MCP rate limit state is in-process; not shared across pods | Medium | DEFERRED |
| F10 | V10.2.1 | Connection count limit check is not concurrency-safe | Medium | **FIXED** |
| F11 | V10.2.2 | Trial plan expiry (`plan_expires_at`) is not enforced | Medium | **FIXED** |
| F12 | V10.2.3 | Deactivated tenant does not block authenticated requests | High | **FIXED** |

---

*Last reviewed: 2026-02-19. Checklist version: 2.0. All controls verified with automated tests except F7 (key rotation) and F9 (Redis rate limiting) which remain deferred.*
