# Security Verification Checklist

**Standard:** OWASP Application Security Verification Standard (ASVS) v4.0 — adapted for this multi-tenant SaaS application.

**Scope:** FastAPI backend (`backend/app/`), PostgreSQL RLS, Celery workers, MCP tool server.

**How to use:** Work through each section. For each item, run the suggested test and mark `[x]` when the control is confirmed to be in place. Items marked with `FINDING` indicate a known gap identified during initial authorship — prioritise these.

---

## V2 — Authentication

### V2.1 Password Security

- [ ] **V2.1.1** Passwords are hashed with bcrypt using `bcrypt.gensalt()` (work factor >= 10) before being stored.
  - **Test:** Insert a user via `POST /api/v1/auth/register` and inspect `users.hashed_password` directly in the DB. Confirm it starts with `$2b$` and the cost factor is visible in the hash prefix (e.g. `$2b$12$`).
  - **Code:** `backend/app/core/security.py` — `hash_password()` / `verify_password()`.

- [ ] **V2.1.2** Plaintext passwords are never stored, logged, or returned in any response body or error message.
  - **Test:** Trigger a failed login with a bad password and capture the full response body and server logs. Confirm the submitted password does not appear. Search structured logs for the string `password` key containing non-hashed values: `grep -r '"password"' logs/`.
  - **Test:** Call `GET /api/v1/auth/me` — confirm `hashed_password` is absent from the `UserProfile` response schema (`backend/app/schemas/auth.py`).

- [ ] **V2.1.3** Minimum password length of 8 characters is enforced at the API layer before any processing.
  - **Test:** Send `POST /api/v1/auth/register` with `"password": "short"` (7 chars). Expect HTTP 422 Unprocessable Entity.
  - **Code:** `backend/app/schemas/auth.py` — `RegisterRequest.password` field: `Field(min_length=8, max_length=128)`.

- [ ] **V2.1.4** Maximum password length of 128 characters is enforced to prevent bcrypt truncation attacks (bcrypt silently truncates at 72 bytes).
  - **Test:** Send a registration request with a 200-character password. Expect HTTP 422.
  - **Code:** `backend/app/schemas/auth.py` — `Field(min_length=8, max_length=128)`.

- [x] **V2.1.5 (FIXED — F1)** Password complexity policy enforced: `@field_validator("password")` in `RegisterRequest` requires at least one uppercase letter, one digit, and one special character.
  - **Code:** `backend/app/schemas/auth.py` — `RegisterRequest.password_complexity()`.
  - **Test:** `backend/tests/test_auth_security.py::TestPasswordComplexity` — validates 422 for missing digit, missing special, missing uppercase; 201 for valid complex password.

### V2.2 General Authenticator Security

- [x] **V2.2.1 (FIXED — F2)** Login rate limiting is applied: no more than 10 attempts per minute per IP via in-memory sliding-window rate limiter.
  - **Code:** `backend/app/core/rate_limit.py` — `check_login_rate_limit()`. Applied as a check in `backend/app/api/v1/auth.py` — `login()`.
  - **Test:** `backend/tests/test_auth_security.py::TestLoginRateLimit` — sends 11 rapid requests, asserts 429 on the 11th.
  - **Note:** Per-process only. For multi-pod production, use API gateway rate limiting (F9 deferred).

- [ ] **V2.2.2** Authentication failure responses are generic and do not distinguish between "user not found" and "wrong password" (prevents user enumeration).
  - **Test:** Send login requests for (a) a nonexistent email and (b) a valid email with a wrong password. Both must return the identical HTTP 401 body: `{"detail": "Invalid email or password"}`.
  - **Code:** `backend/app/services/auth_service.py` — `authenticate()` iterates candidates and raises a single `ValueError("Invalid email or password")` regardless of the failure reason.

- [ ] **V2.2.3** The `is_active` flag is checked on every authentication and on every token validation, so deactivated users cannot log in or use existing tokens.
  - **Test:** Deactivate a user (`UPDATE users SET is_active = false WHERE id = '<uuid>'`). Attempt login — expect 401. Attempt to call a protected endpoint with a previously-valid access token — expect 401.
  - **Code:** `backend/app/services/auth_service.py` — `authenticate()`: `.where(User.is_active.is_(True))`. `backend/app/core/dependencies.py` — `get_current_user()`: `.where(User.is_active.is_(True))`.

- [ ] **V2.2.4** Tenant registration events are written to the audit log.
  - **Test:** Register a new tenant and query `SELECT * FROM audit_events WHERE action = 'tenant.register'`. Confirm a row exists with the correct `tenant_id` and `actor_id`.
  - **Code:** `backend/app/api/v1/auth.py` — `register()` calls `audit_service.log_event(action="tenant.register")`.

- [ ] **V2.2.5** Login events are written to the audit log.
  - **Test:** Log in successfully and query `SELECT * FROM audit_events WHERE action = 'user.login'`. Confirm a row exists.
  - **Code:** `backend/app/api/v1/auth.py` — `login()` calls `audit_service.log_event(action="user.login")`.

### V2.3 Authenticator Lifecycle

- [ ] **V2.3.1** Tenant switch requires the requesting user to already hold a valid JWT (i.e. is authenticated) and to have a matching email account in the target tenant.
  - **Test:** Call `POST /api/v1/auth/switch-tenant` without a Bearer token. Expect 403. Call with a valid token but a tenant_id where the user's email has no account. Expect 403 with `"You do not have an account in that tenant"`.
  - **Code:** `backend/app/api/v1/auth.py` — `switch_tenant()` depends on `get_current_user`. `backend/app/services/auth_service.py` — `switch_tenant()` filters by `User.email == email` and `User.tenant_id == target`.

---

## V3 — Session Management

### V3.1 Token Storage and Transmission

- [ ] **V3.1.1** Access tokens are short-lived (30 minutes) and the expiry claim (`exp`) is validated on every request.
  - **Test:** Create an access token, wait 31 minutes (or manually set `exp` to a past timestamp using the JWT secret in a test environment), then call a protected endpoint. Expect 401.
  - **Code:** `backend/app/core/security.py` — `create_access_token()` sets `exp = now + timedelta(minutes=30)`. `decode_token()` uses `jwt.decode()` which validates `exp` by default.

- [ ] **V3.1.2** Refresh tokens are long-lived (7 days) and are validated for both expiry and token type before issuing a new access token.
  - **Test:** Call `POST /api/v1/auth/refresh` with an access token string instead of a refresh token. Expect 401 (`Invalid refresh token` — because `payload.get("type") != "refresh"`).
  - **Code:** `backend/app/services/auth_service.py` — `refresh_access_token()` checks `payload.get("type") != "refresh"`.

- [x] **V3.1.3 (FIXED — F3)** Refresh tokens are set as `HttpOnly; Secure; SameSite=Lax` cookies on login, register, refresh, and switch-tenant responses. Removed from JSON body.
  - **Code:** `backend/app/api/v1/auth.py` — `_set_refresh_cookie()` helper. `POST /api/v1/auth/refresh` reads from cookie first, falls back to body for backward compatibility. Frontend updated: `credentials: "include"` on all fetch calls, removed `refresh_token` from localStorage.
  - **Test:** `backend/tests/test_auth_security.py::TestRefreshTokenCookie` — verifies cookie is set and body is empty.

- [ ] **V3.1.4** Access tokens are transmitted only via the `Authorization: Bearer` header, never as URL query parameters.
  - **Test:** Review all API client code and test cases. Attempt to call `GET /api/v1/auth/me?token=<jwt>` — expect 401/422 (the `HTTPBearer` scheme does not parse query params).
  - **Code:** `backend/app/core/dependencies.py` uses `HTTPBearer()` which reads from the `Authorization` header only.

### V3.2 Token Revocation

- [x] **V3.2.1 (FIXED — F4)** In-memory JWT denylist implemented keyed by `jti` (JWT ID). Tokens include a `jti` claim. `decode_token()` checks the denylist and returns `None` for revoked tokens. Expired entries are automatically cleaned up.
  - **Code:** `backend/app/core/token_denylist.py` — `revoke_token()`, `is_revoked()`. `backend/app/core/security.py` — `create_access_token()` and `create_refresh_token()` now include `jti`. `decode_token()` checks denylist.
  - **Test:** `backend/tests/test_auth_security.py::TestJWTDenylist` — revokes a JTI and asserts 401.
  - **Note:** In-memory only (single process). For multi-pod production, migrate to Redis-backed denylist.

- [x] **V3.2.2 (FIXED — F5)** Logout endpoint implemented. `POST /api/v1/auth/logout` revokes the access token JTI, clears the refresh cookie, and creates an audit event.
  - **Code:** `backend/app/api/v1/auth.py` — `logout()`. Accepts optional `refresh_token_jti` in body to revoke refresh token too.
  - **Test:** `backend/tests/test_auth_security.py::TestLogout` — verifies token is revoked and subsequent requests fail with 401.

### V3.3 Token Content

- [ ] **V3.3.1** JWT payload contains only `sub` (user UUID), `tenant_id`, `exp`, and `type`. No PII (email, full name) is embedded in the token.
  - **Test:** Decode a sample access token (base64 the middle segment). Confirm payload is `{"sub": "<uuid>", "tenant_id": "<uuid>", "exp": <ts>, "type": "access"}`.
  - **Code:** `backend/app/services/auth_service.py` — `_create_tokens()`: `token_data = {"sub": str(user.id), "tenant_id": str(user.tenant_id)}`.

- [ ] **V3.3.2** JWT algorithm is explicitly allowlisted at decode time to prevent the `alg: none` attack.
  - **Test:** Forge a token with `"alg": "none"` in the header and call a protected endpoint. Expect 401.
  - **Code:** `backend/app/core/security.py` — `decode_token()`: `jwt.decode(..., algorithms=[settings.JWT_ALGORITHM])`. The `algorithms` list constrains which algorithm is accepted.

---

## V4 — Access Control

### V4.1 RBAC — Permission Enforcement

- [ ] **V4.1.1** Every protected endpoint depends on `require_permission(codename)` or `get_current_user`, and no route is publicly accessible without authentication except `/api/v1/auth/login`, `/api/v1/auth/register`, and `/api/v1/health`.
  - **Test:** List all routes via `GET /openapi.json`. For each route not in the public allowlist, attempt a request without an `Authorization` header. All should return 403.
  - **Code:** `backend/app/core/dependencies.py` — `require_permission()` wraps `get_current_user`.

- [ ] **V4.1.2** The `readonly` role can only read connections, tables, and audit events — it cannot create, modify, or delete any resource.
  - **Test:** Log in as a user with only the `readonly` role. Attempt `POST /api/v1/connections` and `DELETE /api/v1/connections/{id}`. Both must return 403.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `ROLE_PERMISSIONS["readonly"] = ["connections.view", "tables.view", "audit.view"]`.

- [ ] **V4.1.3** The `finance` role cannot manage connections or users.
  - **Test:** Log in as `finance` role user. Attempt `POST /api/v1/connections` and `POST /api/v1/users`. Both must return 403.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `ROLE_PERMISSIONS["finance"]` does not include `connections.manage` or `users.manage`.

- [ ] **V4.1.4** The `ops` role cannot view audit logs.
  - **Test:** Log in as `ops` role user. Attempt `GET /api/v1/audit`. Expect 403 (no `audit.view` permission).
  - **FINDING:** Verify the audit endpoint uses `require_permission("audit.view")`. Check `backend/app/api/v1/audit.py`.

- [ ] **V4.1.5** Permission checks query the database at request time — permissions are not cached in the JWT itself. Role changes take effect immediately without requiring re-login.
  - **Test:** Revoke a permission from a user's role in the DB (`DELETE FROM role_permissions WHERE ...`). Within the same token's lifetime, call an endpoint requiring that permission. Expect 403.
  - **Code:** `backend/app/core/dependencies.py` — `require_permission()` executes a live DB query on every call: `select(Permission.codename).join(RolePermission).where(RolePermission.role_id.in_(role_ids))`.

- [ ] **V4.1.6** A user with no roles assigned cannot access any permission-gated endpoint.
  - **Test:** Create a user, do not assign any role, log in, call `GET /api/v1/connections`. Expect 403 with `"No roles assigned"`.
  - **Code:** `backend/app/core/dependencies.py` — `require_permission()` checks `if not role_ids: raise HTTP 403`.

### V4.2 Tenant Isolation — Row-Level Security

- [ ] **V4.2.1** PostgreSQL RLS is enabled on all tenant-scoped tables. Verify the policy names and that `FORCE ROW LEVEL SECURITY` is applied (or that the app user is not a superuser/table owner that bypasses RLS).
  - **Test:** As the application DB user (`psql`): run `SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public'`. All tables in `RLS_TABLES` must show `rowsecurity = true`.
  - **Test:** Without setting `app.current_tenant_id`, run a direct `SELECT` against `users` as the app DB role. Expect 0 rows (policy blocks access when setting is absent).
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `RLS_TABLES` list and the loop calling `ENABLE ROW LEVEL SECURITY` + `CREATE POLICY`.

- [ ] **V4.2.2** `SET LOCAL app.current_tenant_id` is called inside the request's database transaction (using `SET LOCAL`, not `SET`) so the context is cleared when the transaction ends or rolls back.
  - **Test:** Confirm by inspecting the DB session: after a request completes, open a new connection and `SHOW app.current_tenant_id` — it should return an empty string or raise `unrecognized configuration parameter`.
  - **Code:** `backend/app/core/dependencies.py` — `get_current_user()`: `await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))`. The `LOCAL` keyword scopes the setting to the current transaction.

- [ ] **V4.2.3** Cross-tenant data access is impossible via the API. A user from Tenant A cannot read, modify, or delete resources belonging to Tenant B.
  - **Test:** As Tenant A user, call `GET /api/v1/connections/{connection_id}` where `connection_id` is a valid connection owned by Tenant B. Expect 404 (RLS filters the row; the app sees no result and returns not-found, not a 403 that would confirm the resource exists).
  - **Code:** `backend/app/services/connection_service.py` — `list_connections()` and `delete_connection()` both include `Connection.tenant_id == tenant_id` in the WHERE clause as an application-layer guard in addition to RLS.

- [ ] **V4.2.4** The `audit_events` table has separate `SELECT` and `INSERT` RLS policies with no `UPDATE` or `DELETE` policy — audit records cannot be modified or deleted by any application-level DB statement.
  - **Test:** As the app DB user, attempt `UPDATE audit_events SET action = 'tampered' WHERE id = 1`. Expect 0 rows affected (no UPDATE policy exists, so RLS blocks all updates).
  - **Test:** Attempt `DELETE FROM audit_events WHERE id = 1`. Expect 0 rows affected.
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `audit_events_select` (FOR SELECT) and `audit_events_insert` (FOR INSERT WITH CHECK) policies. No UPDATE or DELETE policy is created.

- [x] **V4.2.5 (FIXED — F6)** Celery workers set RLS context via `tenant_session()` in `backend/app/workers/base_task.py`, which calls `SET LOCAL app.current_tenant_id`. All worker tasks inherit from `InstrumentedTask` and use `tenant_session()`. Confirmed in code review — no bare sessions.
  - **Code:** `backend/app/workers/base_task.py` — `tenant_session()` context manager.

### V4.3 Entitlement Enforcement

- [ ] **V4.3.1** The `trial` plan is blocked from using MCP tools entirely.
  - **Test:** Register a new tenant (starts on `trial` plan). Call any MCP-tool-backed endpoint that checks `require_entitlement("mcp_tools")`. Expect 403 with `"Feature 'mcp_tools' not available on trial plan"`.
  - **Code:** `backend/app/core/dependencies.py` — `require_entitlement()` checks `plan_features["trial"]["mcp_tools"] == False`.

- [ ] **V4.3.2** Connection limits are enforced per plan before a new connection is created.
  - **Test:** On a `trial` account, create 2 non-NetSuite connections (reaching the limit). Attempt to create a third. Expect 403 with `"Connection limit reached for your plan"`.
  - **Code:** `backend/app/api/v1/connections.py` — `create_connection()` calls `entitlement_service.check_entitlement(db, tenant_id, "connections")`. `backend/app/services/entitlement_service.py` — `PLAN_LIMITS["trial"]["max_connections"] = 2`.

- [ ] **V4.3.3** NetSuite connections are exempt from the per-plan connection count limit.
  - **Test:** On a `trial` account at the 2-connection limit, create a NetSuite connection. Expect 201 Created.
  - **Code:** `backend/app/services/entitlement_service.py` — `check_entitlement()` filters `Connection.provider != "netsuite"` when counting toward the limit.

---

## V6 — Data Protection

### V6.1 Credential Encryption at Rest

- [ ] **V6.1.1** Third-party credentials (Shopify API key, Stripe secret key, NetSuite OAuth tokens) are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before being written to `connections.encrypted_credentials`.
  - **Test:** Create a connection via the API. Query `SELECT encrypted_credentials FROM connections WHERE ...` directly in the DB. The value must be a base64url-encoded Fernet token (starts with `gAAAAA`), not a readable JSON string.
  - **Code:** `backend/app/services/connection_service.py` — `create_connection()` calls `encrypt_credentials(credentials)`. `backend/app/core/encryption.py` — `encrypt_credentials()`.

- [ ] **V6.1.2** The `ENCRYPTION_KEY` is a valid Fernet key read from an environment variable. The default placeholder value `"change-me-generate-a-real-fernet-key"` is rejected at startup in non-development environments.
  - **Test:** Launch the application with `ENCRYPTION_KEY=change-me-generate-a-real-fernet-key` and attempt to create a connection. Confirm `_get_fernet()` raises `ValueError("ENCRYPTION_KEY must be set to a valid Fernet key")`.
  - **Test:** In production, confirm `ENCRYPTION_KEY` is injected from a secrets manager (Vault, AWS Secrets Manager, etc.) and is never committed to the repository. Run `git log -p -- .env` to verify.
  - **Code:** `backend/app/core/encryption.py` — `_get_fernet()` guard clause.

- [ ] **V6.1.3** Credential key versioning is tracked: `connections.encryption_key_version` stores the integer version of the key used at encryption time, enabling key rotation without immediate re-encryption of all records.
  - **Test:** Check that `encryption_key_version` is populated on every new connection: `SELECT encryption_key_version FROM connections LIMIT 5`. All rows should match `settings.ENCRYPTION_KEY_VERSION`.
  - **Code:** `backend/app/services/connection_service.py` — `create_connection()` sets `encryption_key_version=get_current_key_version()`. `backend/app/models/connection.py` — `encryption_key_version` column.

- [ ] **V6.1.4 (DEFERRED — F7)** No key rotation procedure implemented. Documented as tech debt. Future: add `kid` claim to tokens for rotation support and implement a re-encryption management command.
  - **Remediation:** Implement a management command for key rotation. Low urgency for single-key deployments.

### V6.2 Sensitive Data in API Responses

- [ ] **V6.2.1** The `ConnectionResponse` schema never includes `encrypted_credentials` or decrypted credential fields.
  - **Test:** Call `GET /api/v1/connections`. Inspect the response body. Confirm no field named `encrypted_credentials`, `api_key`, `secret`, `token`, `password`, or similar is present.
  - **Code:** `backend/app/schemas/connection.py` — `ConnectionResponse` must not map `encrypted_credentials`. `backend/app/api/v1/connections.py` — `list_connections()` builds `ConnectionResponse` without credentials.

- [ ] **V6.2.2** The `UserProfile` schema does not expose `hashed_password` or any internal user fields beyond `id`, `tenant_id`, `email`, `full_name`, `actor_type`, and `roles`.
  - **Test:** Call `GET /api/v1/auth/me`. Confirm `hashed_password` is absent.
  - **Code:** `backend/app/schemas/auth.py` — `UserProfile` fields.

- [ ] **V6.2.3** MCP tool results are redacted before being returned or logged: fields named `password`, `secret`, `token`, `api_key`, `credentials` are replaced with `"***REDACTED***"`.
  - **Test:** In a test environment, configure a tool to return a dict containing `{"api_key": "sk-live-1234", "data": "ok"}`. Confirm the caller receives `{"api_key": "***REDACTED***", "data": "ok"}`.
  - **Code:** `backend/app/mcp/governance.py` — `redact_result()`.

### V6.3 Secrets in Logs

- [ ] **V6.3.1** No plaintext secrets, passwords, or credential values appear in structured logs.
  - **Test:** Enable debug logging and perform a full sync cycle. Grep log output for `api_key`, `secret`, `access_token` (the raw value, not the key name): `grep -E '"(api_key|secret|access_token)"\s*:\s*"[^*]' logs/app.log`. Expect zero matches with actual secret values.
  - **Code:** `backend/app/mcp/governance.py` — `create_audit_payload()` excludes `password`, `secret`, `token` from the logged params dict. `backend/app/core/dependencies.py` — structured log context binds only `tenant_id` and `user_id`, not credentials.

- [ ] **V6.3.2** `APP_DEBUG=True` does not cause SQLAlchemy to echo raw SQL that contains credential values in production.
  - **Test:** Confirm `settings.APP_DEBUG = False` in production config. Check `backend/app/core/database.py` — `echo=settings.APP_DEBUG`. If `APP_DEBUG=True` in production, SQL containing Fernet ciphertext could appear in logs (though not plaintext secrets).

- [ ] **V6.3.3** The `JWT_SECRET_KEY` default value `"change-me-in-production"` must not be used in any non-local environment.
  - **Test:** In staging and production, verify `JWT_SECRET_KEY` is set to a cryptographically random value of at least 32 bytes. Check deployment manifests / CI secrets. Run `git log -p -- .env*` to ensure the secret was never committed.

---

## V7 — Audit and Logging

### V7.1 Audit Event Coverage

- [ ] **V7.1.1** The following security-significant events are recorded in `audit_events`: `tenant.register`, `user.login`, `user.switch_tenant`, `connection.create`, `connection.delete`, `job.start`, `job.complete`, `job.failed`, MCP `tool.*` calls (success, error, rate_limited).
  - **Test:** Trigger each event type and query `SELECT action, status, actor_id, tenant_id FROM audit_events ORDER BY id DESC LIMIT 20`. Confirm each event appears with the correct `action` string and `tenant_id`.

- [x] **V7.1.2 (FIXED — F8)** Failed authentication attempts are now logged to `audit_events` with `action="user.login_failed"`, `status="denied"`, and payload containing email and IP.
  - **Code:** `backend/app/api/v1/auth.py` — `login()` catches `ValueError` and logs audit event before re-raising.
  - **Test:** `backend/tests/test_auth_security.py::TestAuditFailedLogin` — verifies audit event exists with correct fields.

- [ ] **V7.1.3** Audit events include `correlation_id`, `actor_id`, `actor_type`, `resource_type`, `resource_id`, and `tenant_id` — enough context to reconstruct a full request chain.
  - **Test:** Pick any audit event and confirm all six fields are non-null (or appropriately nullable for system-actor events where `actor_id` may be null).
  - **Code:** `backend/app/models/audit.py` — `AuditEvent` model fields. `backend/app/services/audit_service.py` — `log_event()` signature.

- [ ] **V7.1.4** MCP tool rate-limit denials are audited in addition to being logged.
  - **Test:** Exceed a tool's rate limit. Query `SELECT * FROM audit_events WHERE action = 'tool.rate_limited'`. Confirm a record with `status="denied"` exists.
  - **Code:** `backend/app/mcp/governance.py` — `governed_execute()` calls `audit_service.log_event(action="tool.rate_limited", status="denied")` on rate limit rejection.

### V7.2 Correlation ID Propagation

- [ ] **V7.2.1** Every HTTP response includes an `X-Correlation-ID` header. If the client provided one, the same value is echoed back. If not, a UUID is generated server-side.
  - **Test:** Send a request with `X-Correlation-ID: test-id-123`. Confirm the response header `X-Correlation-ID: test-id-123` is present. Send a request without the header and confirm a UUID is returned.
  - **Code:** `backend/app/core/middleware.py` — `CorrelationIdMiddleware`.

- [ ] **V7.2.2** The `correlation_id` is bound to the structured logging context for the duration of the request and appears in every log line emitted during that request.
  - **Test:** Trigger a request that generates multiple log lines (e.g., an MCP tool call). Confirm all log lines share the same `correlation_id` value.
  - **Code:** `backend/app/core/middleware.py` — `structlog.contextvars.bind_contextvars(correlation_id=correlation_id)`. `backend/app/core/logging.py` — `structlog.contextvars.merge_contextvars` processor ensures context vars are merged into every log event.

- [ ] **V7.2.3** Celery worker tasks receive and propagate `correlation_id` from the dispatching HTTP request.
  - **Test:** Trigger a sync via `POST /api/v1/sync/shopify`. Capture the `X-Correlation-ID` from the response. Query `SELECT correlation_id FROM audit_events WHERE action IN ('job.start', 'job.complete')` and confirm the ID matches.
  - **Code:** `backend/app/workers/base_task.py` — `InstrumentedTask.before_start()` extracts `correlation_id` from `kwargs` and stores it as `self._correlation_id`, which is then passed to all `AuditEvent` inserts.

### V7.3 Log Integrity and Sensitive Data Scrubbing

- [ ] **V7.3.1** Structured logs are emitted as JSON (one object per line) to make them machine-parseable and compatible with log aggregation pipelines (e.g., CloudWatch, Datadog).
  - **Test:** Capture a block of application logs. Confirm each line is valid JSON: `cat app.log | python -c "import sys, json; [json.loads(l) for l in sys.stdin]"` — no exceptions.
  - **Code:** `backend/app/core/logging.py` — `structlog.processors.JSONRenderer()` is the final processor in the chain.

- [ ] **V7.3.2** The `audit_events` table does not contain decrypted credential values in the `payload` JSON column.
  - **Test:** After creating a connection, query `SELECT payload FROM audit_events WHERE action = 'connection.create'`. The payload should show only `{"provider": "shopify", "label": "..."}`, not the raw credentials dict.
  - **Code:** `backend/app/api/v1/connections.py` — `create_connection()` logs `payload={"provider": request.provider, "label": request.label}` — not `request.credentials`.

---

## V13 — API Security

### V13.1 Input Validation

- [ ] **V13.1.1** All request bodies are validated by Pydantic schemas before reaching service or model layer code. FastAPI returns HTTP 422 with field-level error details for schema violations.
  - **Test:** Send malformed JSON to any POST endpoint. Expect 422. Send a valid-JSON body with a wrong field type (e.g., `"limit": "not-a-number"`). Expect 422.

- [ ] **V13.1.2** The tenant slug field is validated against the pattern `^[a-z0-9-]+$` to prevent path-traversal or injection characters in slug-based lookups.
  - **Test:** Send `POST /api/v1/auth/register` with `"tenant_slug": "../../etc/passwd"`. Expect 422.
  - **Code:** `backend/app/schemas/auth.py` — `tenant_slug: str = Field(min_length=2, max_length=255, pattern=r"^[a-z0-9-]+$")`.

- [ ] **V13.1.3** SuiteQL query strings passed to the NetSuite MCP tool are not constructed by string interpolation of user input; the query is passed as a parameterised value to the NetSuite REST API.
  - **Test:** Send a tool call with `"query": "SELECT id FROM transaction; DROP TABLE transaction--"`. Verify the NetSuite client treats the entire string as the query body and the NetSuite API rejects it as a syntax error, not a SQL injection.
  - **FINDING:** Review `backend/app/mcp/tools/netsuite_suiteql.py` to confirm the query is sent as a POST body parameter to the NetSuite SuiteQL endpoint, not interpolated into a URL.

- [ ] **V13.1.4** The `data.sample_table_read` MCP tool restricts reads to allowlisted tables. Arbitrary table names cannot be passed.
  - **Test:** Call the tool with `"table_name": "users"` or `"table_name": "connections"`. Confirm the tool rejects non-allowlisted table names and returns an error rather than executing the read.
  - **Code:** `backend/app/mcp/tools/data_sample.py` — verify an allowlist check is present before the query is built.

### V13.2 CORS

- [ ] **V13.2.1** CORS `allow_origins` is restricted to the frontend's exact origin(s) and does not use the wildcard `*`.
  - **Test:** Send an `OPTIONS` preflight request from a non-allowlisted origin (e.g., `Origin: https://evil.com`). The response must not include `Access-Control-Allow-Origin: https://evil.com` or `*`.
  - **Code:** `backend/app/main.py` — `CORSMiddleware(allow_origins=settings.cors_origins_list)`. In production, `CORS_ORIGINS` env var must be set to the exact production frontend URL.

- [ ] **V13.2.2** `allow_credentials=True` is configured, which is required for `HttpOnly` cookie transport of refresh tokens. Confirm this is not combined with `allow_origins=["*"]` (browsers block credentialed requests to wildcard origins).
  - **Test:** Confirm `CORS_ORIGINS` in production is never `"*"`. The combination `allow_credentials=True` + `allow_origins=["*"]` is invalid and will cause browser errors.
  - **Code:** `backend/app/main.py` — `CORSMiddleware(allow_credentials=True, ...)`.

### V13.3 MCP Tool Rate Limiting

- [ ] **V13.3.1** Each MCP tool has a per-tenant per-minute rate limit defined in `TOOL_CONFIGS`. The most sensitive tools have the tightest limits.
  - **Test:** For `recon.run` (limit 10/min), send 11 requests within 60 seconds from the same tenant. The 11th must return `{"error": "Rate limit exceeded", "tool": "recon.run"}`.
  - **Code:** `backend/app/mcp/governance.py` — `TOOL_CONFIGS` and `check_rate_limit()`.

- [ ] **V13.3.2 (DEFERRED — F9)** MCP tool rate limit state is stored in-process. Per-process rate limiter from F2 is sufficient for single-pod deployments. Production multi-pod should use API gateway rate limiting or Redis sliding window counters.
  - **Remediation:** Replace in-process dict with Redis `ZADD` + `ZREMRANGEBYSCORE` when scaling to multiple pods.

- [ ] **V13.3.3** Unknown MCP tool names are rejected immediately without executing any code.
  - **Test:** Call the MCP server with `"method": "tools/call"` and `"name": "system.exec"`. Expect `{"error": "Unknown tool: system.exec"}`.
  - **Code:** `backend/app/mcp/server.py` — `call_tool()` checks `if tool_name not in self.tools` before calling `governed_execute`.

- [ ] **V13.3.4** MCP tool parameter allowlisting strips any parameters not in `allowlisted_params` before execution, preventing injection of unexpected parameters.
  - **Test:** Call `netsuite.suiteql` with an extra param: `{"query": "SELECT ...", "limit": 10, "exec": "DROP TABLE users"}`. Confirm the `exec` key is absent from the params passed to the tool's `execute()` function.
  - **Code:** `backend/app/mcp/governance.py` — `validate_params()` filters to `{k: v for k, v in params.items() if k in allowed}`.

---

## V10 — Business Logic Security

### V10.1 Idempotency and Deduplication

- [ ] **V10.1.1** Canonical data records (orders, payments, refunds, payouts, payout_lines, disputes) have a `dedupe_key` unique constraint per tenant to prevent duplicate inserts from repeated sync runs.
  - **Test:** Run a Shopify sync twice on the same time window. Query `SELECT COUNT(*) FROM orders WHERE tenant_id = '<uuid>'`. The count must remain identical after the second sync (upsert semantics, not insert-only).
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `UniqueConstraint("tenant_id", "dedupe_key", name="uq_orders_dedupe")` and equivalents for all canonical tables.

- [ ] **V10.1.2** The ingestion services use the `dedupe_key` to detect existing records and skip or update them rather than creating duplicates.
  - **Test:** Inspect `backend/app/services/ingestion/shopify_sync.py` and `stripe_sync.py`. Confirm the upsert logic (`INSERT ... ON CONFLICT DO NOTHING` or equivalent ORM pattern) is present.

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

- [ ] **V10.3.1** Celery task `kwargs` include `tenant_id` so the worker can scope its DB operations to the correct tenant via RLS context.
  - **Test:** Inspect a Celery task dispatch in `backend/app/api/v1/sync.py`. Confirm `tenant_id=str(user.tenant_id)` is passed as a kwarg.
  - **Code:** `backend/app/workers/base_task.py` — `before_start()` reads `tenant_id = kwargs.get("tenant_id")` and uses it to create the `Job` record and `AuditEvent`.

- [ ] **V10.3.2** Job records are always created with the correct `tenant_id` and are protected by RLS so one tenant cannot query or cancel another tenant's jobs.
  - **Test:** As Tenant A, attempt `GET /api/v1/jobs/{job_id}` where `job_id` belongs to Tenant B. Expect 404 (RLS filters the row).
  - **Code:** `backend/alembic/versions/001_initial_schema.py` — `jobs` table is in `RLS_TABLES`.

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

*Last reviewed: 2026-02-17. Checklist version: 1.1. Security hardening sprint: F1-F6, F8, F10-F12 fixed. F7, F9 deferred (infrastructure required).*
