---
name: saas-deployment
description: >
  Patterns for deploying and securing a multi-tenant SaaS application with FastAPI, Next.js,
  PostgreSQL (Supabase), Redis (Upstash), and Docker on GCP. Use this skill when working on
  deployment, CI/CD pipelines, Docker configuration, Alembic migrations, environment setup,
  security hardening, Row-Level Security (RLS), JWT authentication, encryption, rate limiting,
  feature flags, multi-tenant isolation, production configuration, or infrastructure separation.
  Also trigger on mentions of staging vs production, Caddy, GHCR, health checks, rollback,
  or any DevOps-related task for this project.
---

# Multi-Tenant SaaS Deployment & Security

This skill covers the full deployment lifecycle and security architecture for a multi-tenant
SaaS application. The patterns here address real production concerns: tenant data isolation,
credential encryption, migration safety, zero-downtime deploys, and the sharp edges that
cause outages if you don't know about them.

## Infrastructure Architecture

```
Vercel (Next.js frontend)
    ↓ API calls
GCP Compute Engine (FastAPI + Celery workers)
    ↓ Database        ↓ Cache/Queue
Supabase PostgreSQL   Upstash Redis (TLS)
```

| Component | Staging | Production |
|-----------|---------|------------|
| Frontend | Vercel preview | Vercel production (custom domain) |
| Backend | GCP e2-small | GCP e2-medium (separate VM) |
| Database | Supabase free | Supabase Pro (separate project) |
| Redis | Upstash free | Upstash Pro (separate instance) |
| Registry | GHCR (shared) | GHCR (shared, same images) |

Staging and production share container images from GHCR but use completely isolated
databases, Redis instances, and VMs. This prevents any possibility of staging data
leaking into production or vice versa.

## Multi-Tenant Isolation — Three Layers

Every row of tenant data is protected by three independent layers. If any single layer fails,
the other two still prevent cross-tenant data access.

### Layer 1: Database (PostgreSQL RLS)

Every multi-tenant table has a `tenant_id UUID NOT NULL` column. RLS policies enforce
that queries only see rows matching the current tenant:

```sql
-- STABLE function (caches per transaction, parallel-safe)
CREATE OR REPLACE FUNCTION get_current_tenant_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT current_setting('app.current_tenant_id')::uuid $$;

-- Applied to every tenant table
CREATE POLICY table_tenant_isolation ON table_name
    USING (tenant_id = get_current_tenant_id());
ALTER TABLE table_name ENABLE ROW LEVEL SECURITY;
```

RLS is the hard boundary. Even if application code has a bug, raw SQL cannot cross tenants.

### Layer 2: Application (SET LOCAL)

Before every database operation, the middleware sets the tenant context:

```python
async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """SET LOCAL scopes to current transaction only."""
    validated = str(uuid.UUID(str(tenant_id)))  # Validates UUID format
    await session.execute(text(f"SET LOCAL app.current_tenant_id = '{validated}'"))
```

**Critical gotcha:** PostgreSQL `SET LOCAL` does NOT support bind parameters (`$1`).
You must validate the UUID before string interpolation. The `uuid.UUID()` constructor
raises `ValueError` on invalid input, preventing SQL injection. Never use raw f-strings
with user input here.

If `SET LOCAL` is not called, RLS denies ALL rows (safe fail).

### Layer 3: API (JWT Validation)

JWT tokens contain `tenant_id`. The auth middleware validates the token, checks that
the tenant is active, and calls `set_tenant_context()`:

```python
async def get_current_user(credentials, db, request) -> User:
    payload = decode_token(credentials.credentials)
    user = await db.get(User, payload["sub"])
    tenant = await db.get(Tenant, user.tenant_id)

    if not tenant.is_active:
        raise HTTPException(403, "Tenant deactivated")
    if tenant.plan == "free" and tenant.plan_expires_at < now():
        raise HTTPException(403, "Plan expired")

    await set_tenant_context(db, str(user.tenant_id))
    return user
```

## Authentication & Token Management

### JWT Strategy

- **Access token:** 30-minute expiry, contains `sub` (user_id), `tenant_id`, `type: "access"`, unique `jti`
- **Refresh token:** 7-day expiry, same fields with `type: "refresh"`
- Both stored in HttpOnly cookies (not localStorage) to prevent XSS
- Each token has a unique JTI (JWT ID) for individual revocation

### Token Denylist (Redis-backed)

Revoked tokens are stored in Redis with TTL matching the token's remaining lifetime:

```python
def revoke_token(jti: str, exp: float) -> None:
    ttl = max(int(exp - time.time()), 1)
    redis.setex(f"jwt:denied:{jti}", ttl, "1")  # Auto-cleanup via TTL
```

Falls back to in-memory dict if Redis unavailable (development only).
**Must have Redis in production** — in-memory state is lost on restart.

### Rate Limiting (Redis-backed)

Sliding window rate limiter for login attempts (10 per 60 seconds per IP):

```python
def check_login_rate_limit(ip: str) -> bool:
    key = f"ratelimit:login:{ip}"
    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, "-inf", cutoff)  # Remove expired
    pipe.zcard(key)                              # Count remaining
    pipe.zadd(key, {str(now): now})              # Add current
    pipe.expire(key, WINDOW_SECONDS + 1)         # Auto-cleanup
    results = pipe.execute()
    return results[1] < MAX_ATTEMPTS
```

Same Redis dependency: falls back to in-memory in dev, required in production.

## Encryption

### Credentials at Rest (Fernet)

NetSuite OAuth tokens and API keys are encrypted with Fernet symmetric encryption:

```python
from cryptography.fernet import Fernet

def encrypt_credentials(credentials: dict) -> str:
    f = Fernet(settings.ENCRYPTION_KEY.encode())
    return f.encrypt(json.dumps(credentials).encode()).decode()

def decrypt_credentials(encrypted: str) -> dict:
    f = Fernet(settings.ENCRYPTION_KEY.encode())
    return json.loads(f.decrypt(encrypted.encode()).decode())
```

Key rotation is tracked via `ENCRYPTION_KEY_VERSION`. The `reencrypt_tenant.py` script
handles migrating credentials when keys rotate.

### Production Secret Validation

The app refuses to start if `APP_ENV != "development"` and secrets are defaults:

```python
def _validate_production_secrets():
    if settings.APP_ENV != "development":
        if settings.JWT_SECRET_KEY == "change-me-in-production":
            raise RuntimeError("FATAL: JWT_SECRET_KEY is still default")
        if settings.ENCRYPTION_KEY == "change-me-generate-a-real-fernet-key":
            raise RuntimeError("FATAL: ENCRYPTION_KEY is still default")
```

This prevents accidentally deploying with insecure secrets.

## Security Headers & Production Hardening

```python
class SecurityHeadersMiddleware:
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if APP_ENV != "development":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
```

Additional hardening:
- Swagger/ReDoc disabled when `APP_ENV != "development"` (`docs_url=None`, `redoc_url=None`)
- Sentry integration with `send_default_pii=False`
- Correlation ID middleware for request tracing

## Alembic Migration Patterns

### Migration Safety Rules

1. **Revision IDs max 32 chars** — `alembic_version.version_num` is `VARCHAR(32)`. Use short IDs like `039_confidence_score`, not `039_chat_message_confidence_score`.

2. **Migrations run in CI, not container startup** — `entrypoint.sh` does NOT run `alembic upgrade head`. Migrations execute in the deploy workflow before SSH deploy. This prevents race conditions with multiple replicas.

3. **Two databases locally** — `.venv/bin/alembic` runs against Supabase (remote). Docker containers use `postgres:5432` (local). After adding a model column, also run:
   ```bash
   docker exec ecom-netsuite-suites-backend-1 alembic upgrade head
   ```
   Or the backend will crash with `UndefinedColumnError`.

4. **Always include downgrade** — Every migration must have a working `downgrade()` function for rollback.

### CI Migration Safety Check

The deploy pipeline runs a full safety check before deploying:

```yaml
steps:
  - Check for multiple Alembic heads (branch conflicts)
  - alembic upgrade head
  - alembic downgrade -1       # Rollback test
  - alembic upgrade head       # Re-apply (idempotency)
  - Warn on DROP operations    # Destructive op detection
```

### Migration Template

```python
"""042_description.py"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "042_description"  # Keep under 32 chars!
down_revision = "041_user_feedback"

def upgrade() -> None:
    op.add_column("table", sa.Column("field", sa.String(50), nullable=True))

def downgrade() -> None:
    op.drop_column("table", "field")
```

## CI/CD Pipeline

### Workflow Structure

```
Push/PR → CI Checks → Deploy
                        ├── Build GHCR Images (SHA-tagged)
                        ├── Migration Safety Check
                        ├── Deploy Staging (automatic)
                        └── Deploy Production (manual approval)
```

### CI Checks (gate for merge)

- Python lint (Ruff)
- Backend tests (pytest with pgvector + Redis services, coverage >60%)
- Frontend lint (ESLint) + type check (`tsc --noEmit`) + build
- Secret scan (Gitleaks)

### Deploy Staging (automatic on main merge)

1. Run `alembic upgrade head` via SSH
2. Pull new GHCR images
3. Restart containers (`docker compose -f docker-compose.prod.yml up -d`)
4. Health check with 30-second timeout (5 retries, 5s apart)

### Deploy Production (manual approval)

Same steps as staging but requires GitHub environment approval.
Rolling deploy: backend first, health check, then worker.

### Rollback

Manual workflow with image SHA (7-char) and optional migration revision:
1. Verify image exists in GHCR
2. Optionally downgrade migration
3. Pull specified image and restart

## Docker Configuration

### Production Dockerfile

```dockerfile
FROM python:3.11-slim
# Non-root user for security
RUN useradd -m appuser
# System deps cleaned after install
# 2 Uvicorn workers
# No dev entrypoint — migrations run in CI
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### Production Compose

```yaml
services:
  backend:
    image: ghcr.io/org/backend:${IMAGE_TAG}
    ports: ["8000:8000"]
    env_file: .env.production
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/api/v1/health')"]
      interval: 30s

  worker:
    image: ghcr.io/org/backend:${IMAGE_TAG}
    command: celery -A app.celery_app worker --concurrency=2 -Q default,sync,recon,export
    restart: unless-stopped
```

No local Postgres or Redis — production uses Supabase and Upstash.
JSON logging with rotation (10MB max, 3 files).

## Database Connection Patterns

### SSL for Supabase

```python
def _build_connect_args(url: str) -> dict:
    if "supabase" in url:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE  # Supabase chain
        return {"ssl": ssl_ctx}
    return {}
```

### Pool Sizing

- Remote (Supabase): `pool_size=5, max_overflow=5, pool_recycle=300`
- Local (Docker): `pool_size=20, max_overflow=10, pool_recycle=-1`

Prefer `DATABASE_URL_DIRECT` (bypasses PgBouncer) for migrations and direct queries.
Use `DATABASE_URL` (pooled) for normal API traffic.

### Celery Worker Sessions

Each Celery prefork worker must create its own async engine per task. The module-level
engine is bound to the main process event loop and cannot be reused in forked workers.

```python
@asynccontextmanager
async def worker_async_session():
    engine = create_async_engine(db_url, pool_size=2, max_overflow=3)
    factory = async_sessionmaker(engine)
    async with factory() as session:
        yield session
    await engine.dispose()  # Clean up per-task
```

## Feature Flags

TTL-cached feature flag service (60-second cache):

```python
DEFAULT_FLAGS = {
    "chat": True, "workspace": True, "analytics_export": True,
    "mcp_tools": False, "reconciliation": False, "byok_ai": False,
    "custom_branding": False, "custom_domain": False,
}
```

`require_feature(flag_key)` FastAPI dependency returns 403 when disabled.
Default flags seeded on tenant creation via `seed_default_flags()`.

## Environment Variables Reference

```bash
# Core
APP_ENV=production          # development | staging | production
DATABASE_URL=postgresql+asyncpg://...    # Pooled (PgBouncer)
DATABASE_URL_DIRECT=postgresql+asyncpg://... # Direct (migrations)
REDIS_URL=rediss://...      # TLS for Upstash

# Auth (generate unique values!)
JWT_SECRET_KEY=<64-byte-url-safe-token>
ENCRYPTION_KEY=<fernet-key>

# Celery (separate Redis namespaces)
CELERY_BROKER_URL=rediss://...   # /1
CELERY_RESULT_BACKEND=rediss://... # /2

# LLM
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_EMBEDDING_API_KEY=sk-...

# NetSuite OAuth
NETSUITE_OAUTH_CLIENT_ID=...
NETSUITE_OAUTH_REDIRECT_URI=https://api.domain.com/api/v1/connections/netsuite/callback
```

## Domain & HTTPS Setup

### Caddy (Auto-SSL)

For the backend API, Caddy provides automatic HTTPS via Let's Encrypt:

```
api.suitestudio.ai {
    reverse_proxy localhost:8000
}
```

### DNS Records

```
suitestudio.ai     → Vercel (frontend)
api.suitestudio.ai → GCP VM IP (backend via Caddy)
```

### Vercel Custom Domain

Add `suitestudio.ai` in Vercel project settings → Domains.
Configure DNS CNAME to `cname.vercel-dns.com`.
