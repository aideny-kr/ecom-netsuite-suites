# NetSuite Ecommerce Ops Suite

A multi-tenant operations platform that bridges ecommerce payment processors (Stripe, Shopify, etc.) with NetSuite ERP. It ingests orders, payments, refunds, payouts, and disputes into a canonical data model, then reconciles and posts journal entries to NetSuite.

## Key Features

- **Multi-tenant architecture** with row-level security, RBAC, and plan-based entitlements (trial / pro / enterprise)
- **Canonical data model** normalizing orders, payments, refunds, payouts, payout lines, disputes, and NetSuite postings across sources
- **Connection management** with encrypted credential storage (Fernet) for payment processor integrations
- **Reconciliation engine** matching payouts to orders/payments and generating NetSuite journal entries
- **MCP tool server** exposing AI-callable tools (SuiteQL queries, reconciliation runs, report exports, schedule management) with governance controls including rate limiting, parameter validation, and result redaction
- **Background job processing** via Celery with dedicated queues for sync, reconciliation, and export tasks
- **Audit trail** recording every significant action with correlation IDs for end-to-end traceability
- **Dashboard UI** built with Next.js, React Query, TanStack Table, and Radix UI primitives

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 (async) |
| Database | PostgreSQL 16 with pgvector, Alembic migrations |
| Cache / Broker | Redis 7 |
| Task Queue | Celery 5 |
| Frontend | Next.js 14, React 18, TypeScript, Tailwind CSS, Radix UI |
| Auth | JWT (access + refresh tokens), bcrypt password hashing |
| Encryption | Fernet symmetric encryption for stored credentials |
| Observability | structlog with correlation ID middleware |
| MCP | Model Context Protocol server with governance wrapper |
| Infrastructure | Docker Compose, Makefiles |

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env -- at minimum set ENCRYPTION_KEY and JWT_SECRET_KEY for production

# 2. Start all services (Postgres, Redis, backend, worker, frontend)
make up

# 3. Run database migrations
make migrate

# 4. Open the app
# Frontend: http://localhost:3002
# Backend API: http://localhost:8000
# API docs (Swagger): http://localhost:8000/docs
```

For detailed setup instructions see [docs/SETUP.md](docs/SETUP.md).

## Project Structure

```
ecom-netsuite-suites/
  backend/
    app/
      api/v1/          # REST endpoints (auth, tenants, users, connections, tables, jobs, audit, health)
      core/            # Config, database, auth dependencies, encryption, middleware, security
      models/          # SQLAlchemy models (canonical tables, tenant, user, RBAC, audit, jobs, pipeline)
      schemas/         # Pydantic request/response schemas
      services/        # Business logic (auth, audit, connections, entitlements, tables)
      workers/         # Celery app and background tasks
      mcp/             # MCP tool server with governance, registry, and tool implementations
    alembic/           # Database migrations
    tests/             # pytest test suite
  frontend/
    src/
      app/             # Next.js App Router pages (login, register, dashboard, connections, tables, audit)
      components/      # React components (sidebar, data table, dialogs, UI primitives)
      hooks/           # React Query hooks (connections, audit, table data, toast)
      lib/             # API client, types, constants, utilities
      providers/       # Auth and React Query providers
      middleware.ts    # Next.js auth middleware
    e2e/               # Playwright end-to-end tests
  docs/                # Architecture decision records, data model docs, security plans
  docker-compose.yml   # Full-stack development environment
  Makefile             # Development commands
```

## Documentation

- [Setup / Installation Guide](docs/SETUP.md)
- [API Reference](docs/API.md)
- [Architecture Overview](docs/ARCHITECTURE.md)
- [Data Model Overview](docs/DATA_MODEL_OVERVIEW.md)
- [Security Implementation Plan](docs/SECURITY_IMPLEMENTATION_PLAN.md)
- [Service Boundaries](docs/SERVICE_BOUNDARIES.md)
- [Multi-Tenancy ADR](docs/ADR_002_MULTI_TENANCY.md)
- [Observability](docs/OBSERVABILITY_IMPLEMENTATION.md)
- [Tool Governance Checklist](docs/TOOL_GOVERNANCE_CHECKLIST.md)

## Development

```bash
# Run backend locally (without Docker)
make backend-dev

# Run frontend locally
make frontend-dev

# Install dependencies
make install

# Run tests
make test

# Run tests with coverage
make test-cov

# Lint
make lint

# Format code
make format

# Run end-to-end tests
make e2e

# View logs
make logs

# Tear down and clean volumes
make clean
```

## License

Proprietary. All rights reserved.
