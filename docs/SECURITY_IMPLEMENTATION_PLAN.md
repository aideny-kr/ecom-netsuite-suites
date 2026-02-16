# Security Implementation Plan
_Last updated: 2026-02-16_

This document details the security implementation for Phase 1, covering credential encryption, key management, JWT authentication, RLS enforcement, and RBAC.

---

## 1. Fernet Encryption for Credential Storage

### Overview

All third-party credentials (OAuth tokens, API keys) are encrypted at rest using Python's `cryptography.fernet.Fernet` symmetric encryption. Credentials are never stored in plaintext.

### Encryption Flow

```
Plaintext Credentials              Encrypted Blob               Database
(JSON string)                      (Fernet token)               (connections.encrypted_credentials)
        |                                |                              |
        |-- json.dumps() -->             |                              |
        |-- fernet.encrypt(bytes) ------>|                              |
        |                                |-- base64 string ------------>|
        |                                |                              |
```

### Decryption Flow

```
Database                           Encrypted Blob               Plaintext Credentials
(connections.encrypted_credentials) (Fernet token)              (dict)
        |                                |                              |
        |-- read base64 string --------->|                              |
        |                                |-- fernet.decrypt(bytes) ---->|
        |                                |                   json.loads() --> dict
```

### Implementation

```python
from cryptography.fernet import Fernet, MultiFernet
from app.core.config import settings

class CredentialVault:
    """Encrypts and decrypts connection credentials using Fernet."""

    def __init__(self):
        # Support multiple keys for rotation
        self._keys: dict[int, Fernet] = {}
        self._load_keys()

    def _load_keys(self):
        """Load encryption keys. Current key is settings.ENCRYPTION_KEY."""
        current = Fernet(settings.ENCRYPTION_KEY.encode())
        self._keys[settings.ENCRYPTION_KEY_VERSION] = current
        # Previous keys loaded from settings.ENCRYPTION_KEY_PREVIOUS (if set)

    def encrypt(self, plaintext: dict) -> tuple[str, int]:
        """Encrypt credentials. Returns (encrypted_blob, key_version)."""
        data = json.dumps(plaintext).encode("utf-8")
        current_key = self._keys[settings.ENCRYPTION_KEY_VERSION]
        encrypted = current_key.encrypt(data)
        return encrypted.decode("utf-8"), settings.ENCRYPTION_KEY_VERSION

    def decrypt(self, encrypted_blob: str, key_version: int) -> dict:
        """Decrypt credentials using the appropriate key version."""
        key = self._keys[key_version]
        decrypted = key.decrypt(encrypted_blob.encode("utf-8"))
        return json.loads(decrypted.decode("utf-8"))
```

### Storage Schema

| Column | Purpose |
|--------|---------|
| `connections.encrypted_credentials` | Fernet-encrypted JSON blob containing provider credentials |
| `connections.encryption_key_version` | Integer indicating which key was used to encrypt |

### Access Control

- Only the Worker service decrypts credentials (to call external APIs).
- The API server encrypts on connection creation but does not decrypt for read operations.
- API responses never include `encrypted_credentials`; the field is excluded from all response schemas.

---

## 2. Key Rotation Procedure

### Rotation Steps

1. **Generate new Fernet key:**
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Update configuration:**
   ```env
   ENCRYPTION_KEY=<new-key>
   ENCRYPTION_KEY_VERSION=2
   ENCRYPTION_KEY_PREVIOUS=<old-key>
   ENCRYPTION_KEY_VERSION_PREVIOUS=1
   ```

3. **Deploy with both keys active.** The vault loads both keys; new encryptions use the new key, decryptions use the version recorded on each row.

4. **Run re-encryption migration:**
   ```python
   # Re-encrypt all connections with the new key
   async def rotate_encryption_keys():
       connections = await get_all_connections()  # bypass RLS, system operation
       for conn in connections:
           plaintext = vault.decrypt(conn.encrypted_credentials, conn.encryption_key_version)
           new_blob, new_version = vault.encrypt(plaintext)
           conn.encrypted_credentials = new_blob
           conn.encryption_key_version = new_version
           await save(conn)
       # Emit audit event: encryption_key_rotated
   ```

5. **Verify all rows updated** to the new `encryption_key_version`.

6. **Remove old key** from configuration after confirmation.

### Key Versioning Rules

| Rule | Detail |
|------|--------|
| Current key version | Used for all new encryptions |
| Previous key version(s) | Kept for decryption of not-yet-rotated rows |
| Key storage | Environment variables (or secrets manager in production) |
| Rotation audit | `category='security'`, `action='encryption_key_rotated'` audit event |
| Rotation frequency | At least annually, or immediately if key is suspected compromised |

---

## 3. Credential Redaction in API Responses

### Principle

No API endpoint ever returns credential values. Credentials are write-only from the API consumer's perspective.

### Implementation

```python
# Pydantic response schema -- no encrypted_credentials field
class ConnectionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    provider: str
    label: str
    status: str
    encryption_key_version: int  # version only, not the key
    metadata_json: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    # encrypted_credentials is deliberately excluded
```

### Redaction Rules

| Field | API Behavior |
|-------|-------------|
| `encrypted_credentials` | Never included in any response |
| `hashed_password` | Never included in any response |
| `JWT_SECRET_KEY` | Server-side only; never exposed |
| `ENCRYPTION_KEY` | Server-side only; never exposed |
| Connection metadata | Returned as-is (non-sensitive provider metadata) |
| Audit event payloads | Credentials scrubbed before storage; only metadata logged |

### Audit Payload Scrubbing

When logging connection events, credential fields are stripped:

```python
def scrub_credentials(payload: dict) -> dict:
    """Remove sensitive fields before writing to audit_events.payload."""
    sensitive_keys = {"password", "secret", "token", "api_key", "credentials"}
    return {
        k: "***REDACTED***" if any(s in k.lower() for s in sensitive_keys) else v
        for k, v in payload.items()
    }
```

---

## 4. JWT Lifecycle

### Token Types

| Token | Purpose | Lifetime | Storage |
|-------|---------|----------|---------|
| Access Token | Authenticate API requests | 30 minutes | Frontend memory (not localStorage) |
| Refresh Token | Obtain new access tokens | 7 days | HttpOnly secure cookie |

### Access Token Claims

```json
{
  "sub": "<user_id>",
  "tenant_id": "<tenant_id>",
  "email": "user@example.com",
  "roles": ["admin"],
  "actor_type": "user",
  "iat": 1708099200,
  "exp": 1708101000,
  "jti": "<unique-token-id>"
}
```

### Token Flow

```
1. Login: POST /api/v1/auth/login
   Request:  {email, password}
   Response: {access_token, token_type: "bearer"}
   Cookie:   Set-Cookie: refresh_token=<token>; HttpOnly; Secure; SameSite=Strict

2. API Request:
   Header: Authorization: Bearer <access_token>
   -> Middleware validates signature, checks expiry
   -> Extracts tenant_id, user_id, roles
   -> Sets request.state.tenant_id and request.state.user

3. Token Refresh: POST /api/v1/auth/refresh
   Cookie:   refresh_token=<token>
   Response: {access_token (new)}
   Cookie:   Set-Cookie: refresh_token=<new_token>; HttpOnly; Secure; SameSite=Strict

4. Logout: POST /api/v1/auth/logout
   -> Invalidate refresh token (add to deny list or delete from DB)
   -> Clear refresh_token cookie
```

### Security Measures

| Measure | Implementation |
|---------|---------------|
| Signing algorithm | HS256 with `JWT_SECRET_KEY` (256-bit minimum) |
| Token validation | Verify signature, check `exp`, check `iat` not in future |
| Refresh token rotation | Issue new refresh token on each refresh; invalidate old one |
| Refresh token storage | HttpOnly, Secure, SameSite=Strict cookie |
| Access token storage | In-memory only (JavaScript variable, not localStorage/sessionStorage) |
| Token revocation | Refresh tokens tracked in DB; can be individually revoked |
| Rate limiting | Login endpoint rate-limited to 10 attempts per minute per IP |

---

## 5. RLS as Security Boundary

### Policy Pattern

Every multi-tenant table uses the same RLS policy:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table> FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON <table>
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

### Context Setting

```python
# Middleware (runs before every handler)
async def set_tenant_context(session, tenant_id: UUID):
    await session.execute(
        text("SET LOCAL app.current_tenant_id = :tid"),
        {"tid": str(tenant_id)}
    )
```

### Fail-Closed Behavior

If `app.current_tenant_id` is not set:
- `current_setting('app.current_tenant_id')` returns empty string `''`
- Cast to UUID fails or matches no rows
- Result: **zero rows returned** (not all rows)

This is the desired fail-closed behavior.

### Database Roles

| Role | RLS Behavior | Used By |
|------|-------------|---------|
| `app_user` | RLS enforced (FORCE ROW LEVEL SECURITY) | API server, Worker, MCP server |
| `app_migrator` | RLS bypassed (superuser/owner) | Alembic migrations only |
| `app_admin` | RLS bypassed | Admin queries, re-encryption, analytics |

The application never connects as superuser or table owner in production.

---

## 6. RBAC Permission Model

### Role Hierarchy

```
admin
  |-- all permissions
  |
finance
  |-- connections:read
  |-- tables:read
  |-- tables:export
  |-- jobs:read
  |-- jobs:write
  |-- audit:read
  |
ops
  |-- connections:read
  |-- tables:read
  |-- tables:export (CSV only)
  |-- jobs:read
  |
readonly
  |-- tables:read
```

### Permission Matrix

| Permission | admin | finance | ops | readonly |
|-----------|-------|---------|-----|----------|
| `connections:read` | Y | Y | Y | N |
| `connections:write` | Y | N | N | N |
| `tables:read` | Y | Y | Y | Y |
| `tables:export` | Y | Y | Y (CSV) | N |
| `config:read` | Y | Y | N | N |
| `config:write` | Y | N | N | N |
| `users:manage` | Y | N | N | N |
| `audit:read` | Y | Y | N | N |
| `jobs:read` | Y | Y | Y | N |
| `jobs:write` | Y | Y | N | N |
| `mcp_tools:invoke` | Y | Y | N | N |

### Enforcement

```python
# Decorator-based RBAC check
@router.post("/connections")
@require_permission("connections:write")
async def create_connection(request: Request, payload: ConnectionCreate):
    ...

# Implementation
def require_permission(codename: str):
    async def dependency(request: Request):
        user = request.state.user
        if codename not in user.permissions:
            # Emit audit event: permission_denied
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    return Depends(dependency)
```

### Permission Resolution

1. JWT contains `roles` claim (list of role names).
2. Middleware loads permissions for those roles from `role_permissions` (cached in Redis, TTL 5 min).
3. `request.state.user.permissions` contains the set of permission codenames.
4. Endpoint checks if the required codename is in the set.

---

## Security Checklist

| Item | Status | Notes |
|------|--------|-------|
| Credentials encrypted at rest (Fernet) | Phase 1 | `connections.encrypted_credentials` |
| Key rotation support (versioned keys) | Phase 1 | `encryption_key_version` column |
| Credentials never in API responses | Phase 1 | Excluded from Pydantic response models |
| Credentials never in audit logs | Phase 1 | Scrubbed before `audit_events.payload` |
| JWT access tokens (30 min TTL) | Phase 1 | HS256 signed |
| JWT refresh tokens (7 day TTL, HttpOnly) | Phase 1 | Secure cookie, rotation on refresh |
| RLS on all multi-tenant tables | Phase 1 | `SET LOCAL` per session |
| RBAC on all endpoints | Phase 1 | Permission codenames per route |
| Password hashing (bcrypt) | Phase 1 | `users.hashed_password` |
| Rate limiting (login, API) | Phase 1 | Per-IP and per-tenant |
| CORS restricted | Phase 1 | `CORS_ORIGINS` setting |
| HTTPS required | Production | TLS termination at load balancer |
| Secrets in environment variables | Phase 1 | Not in code or config files |
