# NetSuite Ecommerce Ops Suite

A multi-tenant operations platform that bridges ecommerce payment processors (Stripe, Shopify) with NetSuite ERP. Ingests orders, payments, refunds, payouts, and disputes into a canonical data model, reconciles transactions, and posts journal entries to NetSuite — with an AI-powered chat assistant and a SuiteScript development workspace built in.

## Key Features

### AI Chat Assistant
- **Multi-agent orchestrator** with semantic routing — classifies intent via heuristics, falls back to LLM planning for ambiguous queries
- **Specialist agents**: SuiteQL query engineer, RAG documentation search, data analysis, workspace IDE operations
- **Multi-provider LLM support**: OpenAI, Anthropic (Claude), Google Gemini — BYOK (bring your own key) per tenant
- **Tenant-aware entity resolution**: fast NER extraction (Haiku) + pg_trgm fuzzy matching maps natural-language entity names to NetSuite script IDs in sub-100ms
- **MCP tool governance**: read-only SQL enforcement, table allowlist, row limits, parameter validation, result redaction
- **SSE streaming** with collapsible `<thinking>` tags in the UI

### SuiteScript Workspace
- **Browser-based IDE** for SuiteScript 2.1 development with syntax highlighting, file tree, and constellation dependency graph
- **Changeset workflow**: propose patches, review diffs, apply or reject — with file locking to prevent concurrent edits
- **Deploy pipeline**: validate SDF project, run Jest unit tests, deploy to sandbox — all from the UI
- **Contextual AI assistance**: workspace chat panel with current file context injection

### Data Ingestion & Reconciliation
- **Canonical data model** normalizing orders, payments, refunds, payouts, payout lines, disputes, and NetSuite postings
- **Incremental sync** from Stripe and Shopify via Celery workers with cursor-based pagination and idempotent upserts
- **Reconciliation engine** matching payouts to orders/payments and generating NetSuite journal entries
- **Connection management** with Fernet-encrypted credential storage and OAuth 2.0 flows

### NetSuite Integration
- **OAuth 2.0 Authorization Code** flow with automatic token refresh
- **SuiteQL via REST API** with dynamic allowlisting for custom record tables (`customrecord_*`, `customlist_*`)
- **Metadata discovery**: 11 SuiteQL queries per tenant discovering custom fields, record types, subsidiaries, departments, classes, locations
- **Custom RESTlet** for File Cabinet I/O (in-place file updates preserving internal IDs)
- **External MCP connector** to NetSuite's native MCP endpoint for standard table queries

### Platform
- **Multi-tenant architecture** with row-level security (RLS via `STABLE` function wrapper), RBAC, and plan-based entitlements
- **Audit trail** recording every mutation with correlation IDs for end-to-end traceability
- **Policy profiles** controlling data access: sensitivity levels, field blocking, allowed record types, row limits
- **Guided onboarding wizard** with multi-step checklist for tenant setup

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) |
| Database | PostgreSQL 16 with pgvector, pg_trgm, btree_gin; Alembic migrations |
| Cache / Broker | Redis 7 |
| Task Queue | Celery 5 |
| Frontend | Next.js 14 (App Router), React 18, TypeScript strict, Tailwind CSS, shadcn/ui |
| Auth | JWT (access + refresh in HttpOnly cookies), bcrypt |
| Encryption | Fernet symmetric encryption for credentials at rest |
| AI/LLM | OpenAI, Anthropic, Google Gemini adapters; MCP tool protocol |
| SuiteApp | SuiteScript 2.1, SDF (ACCOUNTCUSTOMIZATION) |
| Observability | structlog with correlation ID middleware |
| Testing | pytest (async) + Playwright E2E + Jest (SuiteScript) |
| Infrastructure | Docker Compose |

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env -- at minimum set ENCRYPTION_KEY and JWT_SECRET_KEY

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
      api/v1/            # REST endpoints (23 routers)
      core/              # Config, database, auth, encryption, middleware
      models/            # SQLAlchemy 2.0 models (Mapped[] + mapped_column)
      schemas/           # Pydantic v2 request/response schemas
      services/          # Business logic and domain services
        chat/            # AI orchestrator, agents, LLM adapters, tools
          agents/        # Specialist agents (SuiteQL, RAG, analysis, workspace)
          adapters/      # LLM provider adapters (Anthropic, OpenAI, Gemini)
        ingestion/       # Stripe and Shopify sync services
      workers/           # Celery app and background tasks
      mcp/               # MCP tool server, governance, registry
    alembic/             # Database migrations (25 versions)
    tests/               # pytest async test suite (~190 tests)
  frontend/
    src/
      app/(dashboard)/   # Dashboard pages (chat, workspace, connections, audit, tables, settings)
      components/        # React components (chat UI, workspace IDE, data tables)
        chat/            # Message list, tool call steps, session sidebar
        workspace/       # File tree, constellation view, changeset panel, diff viewer
        ui/              # shadcn/ui primitives
      hooks/             # React Query hooks
      lib/               # API client, types, utilities
    e2e/                 # Playwright E2E tests
  suiteapp/
    src/
      FileCabinet/SuiteScripts/  # RESTlets (file cabinet, mock data)
      Objects/                    # SDF deployment descriptors
    __tests__/                    # Jest unit tests for SuiteScripts
  docs/                  # Architecture docs, ADRs, security plans
  docker-compose.yml     # Full-stack development environment
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
make up              # Start all services
make migrate         # Run database migrations
make backend-dev     # Run backend locally (without Docker)
make frontend-dev    # Run frontend locally
make install         # Install dependencies
make test            # Run tests
make test-cov        # Run tests with coverage
make lint            # Lint
make format          # Format code
make e2e             # Run Playwright E2E tests
make logs            # View logs
make clean           # Tear down and clean volumes
```

## License

Proprietary. All rights reserved.
