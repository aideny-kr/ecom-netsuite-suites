# Setup and Installation Guide

## Prerequisites

- **Docker** and **Docker Compose** (recommended for full-stack development)
- **Python 3.11+** (if running the backend locally)
- **Node.js 20+** and **npm** (if running the frontend locally)
- **PostgreSQL 16** with `uuid-ossp`, `pgcrypto`, and `vector` extensions (if not using Docker)
- **Redis 7** (if not using Docker)

## Option A: Docker Compose (Recommended)

This starts all services: PostgreSQL (with pgvector), Redis, backend API, Celery worker, and frontend.

### 1. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and set at least these values for non-development use:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite` |
| `DATABASE_URL_SYNC` | Sync PostgreSQL connection string (for Alembic) | `postgresql://postgres:postgres@localhost:5432/ecom_netsuite` |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `JWT_SECRET_KEY` | Secret for signing JWTs. **Must change in production.** | `change-me-in-production` |
| `JWT_ALGORITHM` | JWT signing algorithm | `HS256` |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime in minutes | `30` |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token lifetime in days | `7` |
| `ENCRYPTION_KEY` | Fernet key for encrypting stored credentials. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | `change-me-generate-a-real-fernet-key` |
| `ENCRYPTION_KEY_VERSION` | Version tag for key rotation | `1` |
| `APP_ENV` | Environment name (`development`, `staging`, `production`) | `development` |
| `APP_DEBUG` | Enable debug mode | `true` |
| `APP_NAME` | Application display name | `NetSuite Ecommerce Ops Suite` |
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | `http://localhost:3000` |
| `CELERY_BROKER_URL` | Celery broker (Redis) | `redis://localhost:6379/1` |
| `CELERY_RESULT_BACKEND` | Celery result backend (Redis) | `redis://localhost:6379/2` |
| `MCP_SERVER_HOST` | MCP server bind address | `0.0.0.0` |
| `MCP_SERVER_PORT` | MCP server port | `8001` |
| `MCP_RATE_LIMIT_PER_MINUTE` | Global MCP rate limit | `60` |
| `NEXT_PUBLIC_API_URL` | Backend API URL (used by the frontend) | `http://localhost:8000` |

### 2. Start Services

```bash
make up
# or
docker compose up -d
```

This brings up:
- **postgres** on port `5432` (PostgreSQL 16 with pgvector)
- **redis** on port `6379`
- **backend** on port `8000` (FastAPI with hot reload)
- **worker** (Celery worker processing queues: default, sync, recon, export)
- **frontend** on port `3002` (Next.js dev server)

### 3. Run Migrations

```bash
make migrate
# or
docker compose exec backend alembic upgrade head
```

### 4. Verify

- Frontend: http://localhost:3002
- Backend API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Health check: http://localhost:8000/api/v1/health

### 5. Create a New Migration

```bash
make revision msg="add new column"
```

## Option B: Local Development (Without Docker)

Use this if you want to run the backend and/or frontend directly on your machine while using Docker only for Postgres and Redis.

### 1. Start Database and Redis

```bash
docker compose up -d postgres redis
```

### 2. Backend Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run migrations
alembic upgrade head

# Start the API server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Or use the Makefile shortcuts:

```bash
make install        # install backend + frontend deps
make migrate-local  # run migrations locally
make backend-dev    # start backend with uvicorn
```

### 3. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Or:

```bash
make frontend-dev
```

The frontend runs on http://localhost:3000 by default.

### 4. Start a Celery Worker (Optional)

```bash
cd backend
celery -A app.workers.celery_app worker --loglevel=info -Q default,sync,recon,export
```

## Common Commands

| Command | Description |
|---|---|
| `make up` | Start all services via Docker Compose |
| `make down` | Stop all services |
| `make build` | Rebuild Docker images |
| `make migrate` | Run Alembic migrations (Docker) |
| `make migrate-local` | Run Alembic migrations (local) |
| `make revision msg="..."` | Create a new Alembic migration |
| `make test` | Run backend tests with pytest |
| `make test-cov` | Run tests with coverage report |
| `make lint` | Lint backend (ruff) and frontend (eslint) |
| `make format` | Format backend code with ruff |
| `make backend-dev` | Run backend locally with hot reload |
| `make frontend-dev` | Run frontend locally |
| `make e2e` | Run Playwright end-to-end tests |
| `make logs` | Tail Docker Compose logs |
| `make clean` | Tear down containers, remove volumes and build artifacts |

## Troubleshooting

**Port conflicts**: If ports 5432, 6379, 8000, or 3002 are in use, stop the conflicting services or update port mappings in `docker-compose.yml`.

**Database connection errors**: Ensure Postgres is healthy before running migrations. Use `docker compose ps` to check service status.

**Frontend can't reach backend**: Verify `NEXT_PUBLIC_API_URL` is set correctly. When using Docker, the frontend container reaches the backend via `http://localhost:8000` (exposed port).

**Encryption key errors**: Generate a valid Fernet key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
