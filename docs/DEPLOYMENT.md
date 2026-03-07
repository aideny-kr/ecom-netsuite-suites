# Deployment Guide

## Architecture Overview

```
┌─────────────┐     ┌──────────────────────────┐     ┌─────────────┐
│   Vercel     │────▶│   GCP Compute Engine     │────▶│  Supabase   │
│  (Frontend)  │     │  backend + celery worker  │     │ (Postgres)  │
│  Next.js 14  │     │  Docker Compose           │     │  pgvector   │
└─────────────┘     └──────────┬───────────────┘     │  RLS        │
                               │                      └─────────────┘
                               ▼
                        ┌─────────────┐
                        │   Upstash   │
                        │   (Redis)   │
                        │ cache/queue │
                        └─────────────┘
```

| Component | Service | Purpose |
|-----------|---------|---------|
| Frontend | Vercel | Next.js 14, auto-deploy on push |
| Backend API | GCP e2-small | FastAPI (uvicorn, 2 workers) |
| Task Worker | GCP e2-small | Celery (concurrency=2, 4 queues) |
| Database | Supabase | PostgreSQL 16 + pgvector + RLS |
| Cache/Queue | Upstash | Redis (token denylist, rate limit, Celery broker) |
| Container Registry | GHCR | Docker images built in CI |

**Estimated cost:** ~$13/mo (GCP VM). All other services on free tier.

---

## Environments

| Environment | Backend URL | Frontend URL | Database | Deploy Trigger |
|-------------|-------------|--------------|----------|---------------|
| Development | `localhost:8000` | `localhost:3000` | Local Docker Postgres | Manual |
| Staging | `http://34.73.236.64:8000` | Vercel preview | Supabase (shared with dev) | Auto on `main` merge |
| Production | TBD | Vercel production | Supabase (separate project) | Manual approval |

---

## Prerequisites

- GCP account with billing enabled
- Vercel account linked to GitHub
- Supabase account
- Upstash account
- `gcloud` CLI installed (`brew install google-cloud-sdk`)
- Docker installed locally

---

## Infrastructure Setup

### 1. Supabase (PostgreSQL + pgvector)

**New project:**
1. [supabase.com/dashboard](https://supabase.com/dashboard) → New Project
2. Region: `us-east-1` (matches GCP + Vercel)
3. Save the database password securely

**Enable extensions** (SQL Editor):
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";
```

**Connection strings** (Settings → Database → Connection string → URI):
```bash
# Direct connection (port 5432) — for backend + migrations
DATABASE_URL_DIRECT=postgresql+asyncpg://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
DATABASE_URL_DIRECT_SYNC=postgresql://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```

**Run migrations:**
```bash
cd backend
DATABASE_URL_DIRECT_SYNC="postgresql://postgres.[ref]:[pw]@..." \
  .venv/bin/alembic upgrade head
```

**Seed domain knowledge (RAG):**
```bash
cd backend
DATABASE_URL_DIRECT="postgresql+asyncpg://postgres.[ref]:[pw]@..." \
  .venv/bin/python -m scripts.ingest_domain_knowledge
```

### 2. Upstash (Redis)

1. [console.upstash.com](https://console.upstash.com) → Create Database
2. Region: `us-east-1`
3. One database is sufficient — Celery namespaces keys internally

Use the same `rediss://` URL for all three env vars:
```bash
REDIS_URL=rediss://default:TOKEN@your-db.upstash.io:6379
CELERY_BROKER_URL=rediss://default:TOKEN@your-db.upstash.io:6379
CELERY_RESULT_BACKEND=rediss://default:TOKEN@your-db.upstash.io:6379
```

### 3. GCP Compute Engine

**Create VM:**
```bash
gcloud config set project YOUR_PROJECT_ID

gcloud compute instances create ecom-staging \
  --zone=us-east1-b \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --boot-disk-type=pd-ssd \
  --tags=http-server,https-server \
  --metadata=startup-script='#!/bin/bash
curl -fsSL https://get.docker.com | sh
apt-get install -y docker-compose-plugin
mkdir -p /opt/ecom-netsuite'
```

**Open port 8000:**
```bash
gcloud compute firewall-rules create allow-backend \
  --allow=tcp:8000 \
  --target-tags=http-server \
  --description="Allow backend API traffic"
```

**Get VM IP:**
```bash
gcloud compute instances describe ecom-staging \
  --zone=us-east1-b \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
```

**Generate deploy SSH key:**
```bash
ssh-keygen -t ed25519 -f ~/.ssh/ecom-staging-deploy -C "github-actions-deploy" -N ""

# Add to VM
gcloud compute os-login ssh-keys add \
  --key-file=~/.ssh/ecom-staging-deploy.pub \
  --project=YOUR_PROJECT_ID

# Test
gcloud compute ssh ecom-staging --zone=us-east1-b --command="docker --version"
```

**Set up app directory on VM:**
```bash
gcloud compute ssh ecom-staging --zone=us-east1-b

# On the VM:
sudo mkdir -p /opt/ecom-netsuite
sudo chown $USER:$USER /opt/ecom-netsuite
cd /opt/ecom-netsuite

# Create docker-compose.prod.yml (copy from repo)
# Create .env.production (see Environment Variables section below)
```

### 4. Vercel (Frontend)

1. [vercel.com/new](https://vercel.com/new) → Import GitHub repo
2. **Root Directory:** `frontend`
3. **Framework:** Next.js (auto-detected)
4. **Environment Variables:**
   - `NEXT_PUBLIC_API_URL` = `http://YOUR_VM_IP:8000`
5. Deploy

For staging, use Vercel's preview deployments (auto on PRs).
For production, set the custom domain in Vercel dashboard.

---

## Environment Variables

### Generate Secrets

```bash
# JWT secret (64-byte URL-safe token)
python -c "import secrets; print(secrets.token_urlsafe(64))"

# Fernet encryption key (for credentials at rest)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### `.env.production` Template

```bash
# ── App ──
APP_ENV=staging                    # or "production"
APP_DEBUG=false
APP_NAME=NetSuite Ecommerce Ops Suite
CORS_ORIGINS=https://your-app.vercel.app

# ── Database (Supabase) ──
DATABASE_URL=postgresql+asyncpg://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
DATABASE_URL_SYNC=postgresql://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
DATABASE_URL_DIRECT=postgresql+asyncpg://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
DATABASE_URL_DIRECT_SYNC=postgresql://postgres.[ref]:[pw]@aws-0-us-east-1.pooler.supabase.com:5432/postgres

# ── Redis (Upstash) ──
REDIS_URL=rediss://default:TOKEN@your-db.upstash.io:6379
CELERY_BROKER_URL=rediss://default:TOKEN@your-db.upstash.io:6379
CELERY_RESULT_BACKEND=rediss://default:TOKEN@your-db.upstash.io:6379

# ── Auth ──
JWT_SECRET_KEY=<generated-64-byte-token>
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
ENCRYPTION_KEY=<generated-fernet-key>
ENCRYPTION_KEY_VERSION=1

# ── AI/LLM ──
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_AI_PROVIDER=anthropic
OPENAI_EMBEDDING_API_KEY=sk-...
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

# ── NetSuite OAuth ──
NETSUITE_OAUTH_CLIENT_ID=<your-integration-client-id>
NETSUITE_OAUTH_REDIRECT_URI=http://YOUR_VM_IP:8000/api/v1/connections/netsuite/callback
NETSUITE_OAUTH_SCOPE=rest_webservices,restlets
NETSUITE_MCP_OAUTH_CLIENT_ID=<mcp-integration-client-id>
NETSUITE_MCP_OAUTH_SCOPE=mcp

# ── Optional ──
SENTRY_DSN=
STRIPE_API_KEY=
STRIPE_WEBHOOK_SECRET=
BRAVE_SEARCH_API_KEY=
WEB_SEARCH_PROVIDER=duckduckgo     # free, no API key needed
```

### GitHub Actions Secrets

Set these in **Settings → Environments → staging**:

| Secret | Value |
|--------|-------|
| `STAGING_HOST` | VM external IP (e.g., `34.73.236.64`) |
| `STAGING_SSH_USER` | SSH username on VM |
| `STAGING_SSH_KEY` | Contents of `~/.ssh/ecom-staging-deploy` (private key) |
| `STAGING_DATABASE_URL_SYNC` | Supabase sync connection string |

For production, create a `production` environment with:

| Secret | Value |
|--------|-------|
| `PROD_HOST` | Production VM IP |
| `PROD_SSH_USER` | SSH username |
| `PROD_SSH_KEY` | Private key |
| `PROD_DATABASE_URL_SYNC` | Production Supabase sync URL |

Set **required reviewers** on the `production` environment for manual approval.

---

## CI/CD Pipeline

### How It Works

```
Push to main
    │
    ▼
┌─────────┐   ┌──────────────┐   ┌─────────────────┐
│ CI (7   │──▶│ Build Docker │──▶│ Deploy Staging   │ (auto)
│ checks) │   │ + Migration  │   │ migrations +     │
│         │   │ Safety Check │   │ docker pull + up │
└─────────┘   └──────────────┘   └─────────────────┘
                                          │
                                          ▼ (manual dispatch)
                                  ┌─────────────────┐
                                  │ Deploy Prod      │ (manual approval)
                                  │ migrations +     │
                                  │ rolling deploy   │
                                  └─────────────────┘
```

### Workflows

| Workflow | File | Trigger | Purpose |
|----------|------|---------|---------|
| CI | `ci.yml` | Push + PR | Lint, test, build, secret scan |
| Deploy | `deploy.yml` | CI success on main, or manual | Build images → migrate → deploy |
| Rollback | `rollback.yml` | Manual only | Rollback to specific image SHA |

### CI Checks (all must pass)

1. **Python Lint** — Ruff check + format
2. **Backend Tests** — pytest against pgvector + Redis services
3. **Frontend Lint** — ESLint
4. **Frontend Type Check** — `tsc --noEmit`
5. **Frontend Build** — Next.js production build
6. **Secret Scan** — Gitleaks
7. **Required Checks Gate** — Blocks PR merge if any fail

### Deploy Steps

1. **CI Gate** — Only proceeds if CI passed
2. **Build Images** — `Dockerfile.prod` → push to GHCR (`ghcr.io/aideny-kr/ecom-netsuite-suites/backend`)
3. **Migration Safety** — Tests upgrade → downgrade → upgrade, warns on DROP operations
4. **Deploy Staging** (auto) — Run Alembic, SSH pull + restart, health check
5. **Deploy Production** (manual) — Same flow with rolling deploy (backend first, health check, then worker)

### Manual Deploy

```bash
# Trigger from GitHub Actions UI:
# Actions → Deploy → Run workflow → choose staging/production

# Or via CLI:
gh workflow run deploy.yml -f environment=staging
gh workflow run deploy.yml -f environment=production
```

### Rollback

```bash
# Find the SHA to rollback to:
git log --oneline -10

# Trigger rollback (with optional migration downgrade):
gh workflow run rollback.yml \
  -f environment=staging \
  -f image_sha=ab0c025 \
  -f rollback_migration=036
```

---

## Docker Images

### Production Dockerfile (`backend/Dockerfile.prod`)

- Base: `python:3.11-slim`
- Non-root user (`appuser`) for security
- 2 Uvicorn workers
- System deps cleaned after install
- No dev entrypoint (migrations run in CI, not container)

### Docker Compose Production (`docker-compose.prod.yml`)

- **backend**: FastAPI on port 8000, healthcheck every 30s, JSON logging (10MB rotate)
- **worker**: Celery with concurrency=2, queues: default, sync, recon, export
- Both use `.env.production` file
- No database/Redis containers (external services)

---

## First Deploy Checklist

### Staging

- [ ] Supabase project created, extensions enabled
- [ ] Alembic migrations run (`alembic upgrade head`)
- [ ] Domain knowledge seeded (`python -m scripts.ingest_domain_knowledge`)
- [ ] Upstash Redis created, connection string saved
- [ ] GCP VM created with Docker installed
- [ ] SSH key generated and added to VM
- [ ] `.env.production` created on VM at `/opt/ecom-netsuite/`
- [ ] `docker-compose.prod.yml` copied to VM
- [ ] GitHub Environment `staging` created with secrets
- [ ] Deploy workflow triggered and succeeded
- [ ] Health check passes: `curl http://VM_IP:8000/api/v1/health`
- [ ] Vercel frontend deployed with `NEXT_PUBLIC_API_URL` set
- [ ] Frontend loads and connects to backend
- [ ] Test tenant registration works
- [ ] NetSuite OAuth flow completes (redirect URI updated)
- [ ] Chat queries return results

### Production

- [ ] Separate Supabase project (Pro tier recommended)
- [ ] Separate Upstash Redis
- [ ] Separate GCP VM (e2-medium recommended)
- [ ] Production secrets generated (different from staging!)
- [ ] GitHub Environment `production` with required reviewers
- [ ] Tenant data migrated via `scripts/export_tenant.py` → `scripts/import_tenant.py`
- [ ] Credentials re-encrypted via `scripts/reencrypt_tenant.py`
- [ ] Custom domain configured on Vercel
- [ ] HTTPS set up (Caddy or Cloud Load Balancer)
- [ ] `NETSUITE_OAUTH_REDIRECT_URI` updated to production URL
- [ ] `CORS_ORIGINS` updated to production frontend URL
- [ ] Sentry DSN configured
- [ ] Stripe webhooks pointed to production URL

---

## Operations

### Health Check

```bash
curl http://VM_IP:8000/api/v1/health
# Returns: {"status": "ok", "version": "..."}
```

### View Logs

```bash
# SSH into VM
gcloud compute ssh ecom-staging --zone=us-east1-b

# Backend logs
cd /opt/ecom-netsuite
docker compose -f docker-compose.prod.yml logs -f backend

# Worker logs
docker compose -f docker-compose.prod.yml logs -f worker

# Last 100 lines
docker compose -f docker-compose.prod.yml logs --tail=100 backend
```

### Change Environment Variables

```bash
# 1. SSH into the VM
gcloud compute ssh ecom-staging --zone=us-east1-b

# 2. Edit the env file
nano /opt/ecom-netsuite/.env.production

# 3. Restart services to pick up changes
cd /opt/ecom-netsuite
docker compose -f docker-compose.prod.yml restart

# If you changed something that requires a full rebuild (rare):
docker compose -f docker-compose.prod.yml up -d --build
```

Common env changes:
- **CORS_ORIGINS** — add new frontend URLs (comma-separated)
- **API keys** — rotate ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.
- **NETSUITE_OAUTH_REDIRECT_URI** — update when domain changes
- **ENVIRONMENT** — `staging` or `production`

### Restart Services

```bash
# Restart backend only
docker compose -f docker-compose.prod.yml restart backend

# Restart everything
docker compose -f docker-compose.prod.yml restart

# Full rebuild (after image update)
docker compose -f docker-compose.prod.yml up -d --build
```

### Run Migrations Manually

```bash
# From local machine against staging DB:
cd backend
DATABASE_URL_DIRECT_SYNC="postgresql://..." .venv/bin/alembic upgrade head

# Check current revision:
DATABASE_URL_DIRECT_SYNC="postgresql://..." .venv/bin/alembic current
```

### Tenant Data Migration (Staging → Production)

```bash
# Export from staging
python scripts/export_tenant.py \
  --tenant-id bf92d059-... \
  --db-url "postgresql://staging-url" \
  --output tenant_export.json

# Re-encrypt credentials for production Fernet key
python scripts/reencrypt_tenant.py \
  --input tenant_export.json \
  --old-key "STAGING_FERNET_KEY" \
  --new-key "PRODUCTION_FERNET_KEY" \
  --output tenant_export_reencrypted.json

# Import to production
python scripts/import_tenant.py \
  --input tenant_export_reencrypted.json \
  --db-url "postgresql://production-url"
```

---

## HTTPS Setup (Production)

### Option A: Caddy Reverse Proxy

Add to `docker-compose.prod.yml`:
```yaml
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on:
      - backend

volumes:
  caddy_data:
```

Create `Caddyfile`:
```
api.yourdomain.com {
    reverse_proxy backend:8000
}
```

Update:
- `NETSUITE_OAUTH_REDIRECT_URI` → `https://api.yourdomain.com/api/v1/connections/netsuite/callback`
- `CORS_ORIGINS` → `https://your-app.vercel.app`
- `NEXT_PUBLIC_API_URL` → `https://api.yourdomain.com`

### Option B: GCP Load Balancer + Managed SSL

Use GCP's HTTP(S) Load Balancer with a managed SSL certificate. More complex but better for production scale.

---

## Troubleshooting / Gotchas

Lessons learned from the first deployment. Read these before deploying to a new environment.

### 1. Supabase IPv4 Required for External Connections

**Problem:** GCP VM cannot connect to Supabase on free tier — pooler returns `Tenant or user not found`, direct port 5432 times out.

**Cause:** Supabase free tier shared pooler is not IPv4-compatible. GCP VMs use IPv4.

**Fix:** Upgrade Supabase to Pro ($25/mo) and enable the **IPv4 Add-on** ($4/mo) under Settings → Add-ons.

### 2. Supabase SSL Cert Verification Fails in Docker

**Problem:** `SSLCertVerificationError: certificate verify failed: self-signed certificate in certificate chain`

**Cause:** Python `ssl.create_default_context()` validates against the system CA bundle. The `python:3.11-slim` Docker image has a minimal CA bundle that doesn't trust Supabase's certificate chain.

**Fix:** Disable cert verification in `database.py` (connection is still TLS-encrypted):
```python
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
```

### 3. Celery Requires SSL Params for Upstash (rediss://)

**Problem:** Worker crashes with `ValueError: A rediss:// URL must have parameter ssl_cert_reqs`

**Cause:** Celery's Redis backend requires explicit SSL cert params when using `rediss://` URLs.

**Fix:** Append `?ssl_cert_reqs=CERT_NONE` to both Celery URLs:
```bash
CELERY_BROKER_URL=rediss://...@host:6379?ssl_cert_reqs=CERT_NONE
CELERY_RESULT_BACKEND=rediss://...@host:6379?ssl_cert_reqs=CERT_NONE
```
Leave `REDIS_URL` as-is (the app uses `redis.asyncio` which handles SSL differently).

### 4. Docker Permission Denied on Fresh VM

**Problem:** `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`

**Fix:** Add your user to the docker group:
```bash
sudo usermod -aG docker $USER
newgrp docker
```

### 5. GHCR Image Build Needs Buildx

**Problem:** `Cache export is not supported for the docker driver`

**Cause:** `docker/build-push-action` with GHA cache requires the `docker-container` buildx driver.

**Fix:** Add `docker/setup-buildx-action@v3` step before the build step in `deploy.yml`.

### 6. Use Direct Connection (Port 5432), Not Pooler

**Problem:** After enabling IPv4, the direct connection works but pooler still fails.

**Solution:** Use the direct connection string (`db.PROJECT_REF.supabase.co:5432`) for all `DATABASE_URL*` vars in `.env.production`. The pooler (port 6543) uses a different username format (`postgres.PROJECT_REF`) that can be finicky. Direct connection is simpler and fine for low-connection workloads.

### 7. Ruff Import Ordering in CI

**Problem:** CI lint fails on import ordering even though code works locally.

**Cause:** Inline imports (`import x` inside a function) must come before `from x import y` per ruff's isort rules.

**Fix:** Always put stdlib `import` statements before `from ... import` statements, even inside functions.

---

## Security Notes

- **Secrets validation**: App refuses to start in non-development mode with default JWT/encryption keys
- **RLS**: All tables enforce row-level security via `SET LOCAL app.current_tenant_id`
- **Token denylist**: Redis-backed JWT revocation with TTL matching token expiry
- **Rate limiting**: Redis-backed sliding window on login endpoint
- **Security headers**: HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy
- **Swagger disabled**: `/docs` and `/redoc` only available in development
- **SSL**: Supabase connections use TLS (encrypted) with verification disabled in Docker (see Gotcha #2)
- **No migrations on boot**: Alembic runs in CI pipeline, not container startup

---

## Cost Summary

| Service | Tier | Cost |
|---------|------|------|
| Supabase | Pro + IPv4 | $29/mo |
| Upstash | Free (10K commands/day) | $0 |
| Vercel | Free (100GB bandwidth) | $0 |
| GCP e2-small | On-demand | ~$13/mo |
| **Total staging** | | **~$42/mo** |

**Production estimate:** ~$80-120/mo (larger VM + Supabase Pro + Upstash Pro + Vercel Pro)
