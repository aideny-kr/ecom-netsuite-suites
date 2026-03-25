# AI-den — NetSuite AI Operations Platform

A multi-tenant AI platform that connects NetSuite ERP and BigQuery data warehouses to an intelligent chat assistant. Features specialized agent routing, natural language querying, financial reporting with auto-charts, and a SuiteScript development workspace.

## Key Features

### AI Chat Assistant (v1.1)
- **Three-tier agent routing** — Tier 1 regex (<1ms) → Tier 2 Haiku semantic classification (~50ms) → Tier 3 UnifiedAgent fallback
- **Specialized agents**: BI Analyst (BigQuery SQL + charts), Pricing Specialist (margins, tariffs), UnifiedAgent (NetSuite, investigations, general)
- **Investigation mode** — "why" queries get 12-step budget, progressive output, systemnote expertise. Outperforms Claude + native MCP
- **BigQuery BI agent** — natural language → BigQuery Standard SQL → premium recharts visualization (bar, line, pie, area, scatter)
- **Financial report auto-charts** — deterministic post-processing generates grouped bar charts from income statement and balance sheet trends
- **Chart pipeline** — `<chart>` XML extraction from agent text, SSE chart events, recharts frontend renderer with smart $M formatting and gradients
- **Multi-provider LLM support**: Anthropic (Claude), OpenAI, Google Gemini — BYOK (bring your own key) per tenant
- **Entity resolution**: fast NER (Haiku) + pg_trgm fuzzy matching maps entity names to NetSuite script IDs in sub-100ms
- **MCP tool governance**: read-only SQL enforcement, table allowlist, row limits, cost guardrails
- **SSE streaming** with markdown rendering, collapsible `<thinking>` tags, and progressive scroll

### Agent Framework (v1.1)
- **Composition-based architecture** — YAML config + prompt file + HookManager (not deep inheritance)
- **AgentRegistry** with YAML config loading, per-tenant DB overrides, agent instantiation
- **Circuit breaker** — auto-disables agents at 5% error rate over last 100 queries
- **Tool filtering** — each agent's YAML `tool_ids` controls which tools are visible
- **RAG partitions** — per-agent knowledge isolation via `partition_id` on domain_knowledge_chunks
- **Benchmark framework** — pass@5 consistency tests, BI agent vs UnifiedAgent comparison

### BigQuery Integration (v1.1)
- **Service account auth** — encrypted credentials, no OAuth token refresh needed
- **3 local tools**: `bigquery_sql` (read-only, cost-guardrailed), `bigquery_schema` (discover tables/columns), `bigquery_cost_estimate` (dry-run)
- **Connector-gated tools** — BigQuery tools only appear when tenant has active connector
- **Table selector UI** — hierarchical dataset/table browser with search, select/deselect per dataset
- **Schema RAG seeder** — auto-seeds table schemas into RAG partitions on connector creation
- **Regional support** — `location` parameter for us-central1, EU, etc.

### SuiteScript Workspace
- **Browser-based IDE** for SuiteScript 2.1 with syntax highlighting, file tree, and constellation dependency graph
- **Changeset workflow**: propose patches, review diffs, apply or reject with file locking
- **Deploy pipeline**: validate SDF project, run Jest unit tests, deploy to sandbox
- **Contextual AI assistance**: workspace chat panel with current file context injection

### NetSuite Integration
- **OAuth 2.0 PKCE** with per-connection client IDs and proactive token refresh (every 5 min)
- **SuiteQL via REST API** with dynamic allowlisting for custom records
- **External MCP connector** to NetSuite's native MCP endpoint (11 tools)
- **Saved search interception** — ns_runSavedSearch results rendered as DataTable with CSV/Excel export
- **Financial reports** — income statement, balance sheet, trial balance via dedicated tool with pre-computed summaries
- **Metadata discovery**: custom fields, record types, subsidiaries, departments, classes, locations

### Platform
- **Multi-tenant** with row-level security (RLS), RBAC, and plan-based entitlements
- **Audit trail** recording every mutation with correlation IDs
- **White-label branding** — per-tenant brand name, color, logo, favicon
- **Feature flags** — TTL-cached, per-tenant feature gating
- **Email invitations** via Resend with role-based access

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Gunicorn (4 workers) |
| Database | PostgreSQL 16 with pgvector, pg_trgm, btree_gin; 53 Alembic migrations |
| Cache / Broker | Redis 7 |
| Task Queue | Celery 5 |
| Frontend | Next.js 14 (App Router), React 18, TypeScript strict, Tailwind CSS, shadcn/ui, recharts |
| Auth | JWT (access + refresh in HttpOnly cookies), Google OAuth, bcrypt |
| Encryption | Fernet symmetric encryption for credentials at rest |
| AI/LLM | Anthropic Claude, OpenAI, Google Gemini adapters; MCP tool protocol |
| BigQuery | google-cloud-bigquery SDK, service account auth, asyncio.to_thread() |
| SuiteApp | SuiteScript 2.1, SDF (ACCOUNTCUSTOMIZATION) |
| Testing | pytest (async, 2300+ tests), Playwright E2E, Jest (SuiteScript) |
| Infrastructure | Docker Compose, Gunicorn + Uvicorn workers, Cloudflare Tunnel |
| Staging | GCP e2-small, api-staging.suitestudio.ai + staging.suitestudio.ai (Vercel) |

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env — at minimum set ENCRYPTION_KEY and JWT_SECRET_KEY

# 2. Start all services (Postgres, Redis, backend, worker, frontend)
docker compose up -d

# 3. Run database migrations
docker exec ecom-netsuite-suites-backend-1 alembic upgrade head

# 4. Open the app
# Frontend: http://localhost:3002
# Backend API: http://localhost:8000
```

## Project Structure

```
ecom-netsuite-suites/
  backend/
    app/
      api/v1/              # REST endpoints
      core/                # Config, database, auth, encryption, middleware
      models/              # SQLAlchemy 2.0 models (Mapped[] + mapped_column)
      schemas/             # Pydantic v2 request/response schemas
      services/            # Business logic and domain services
        chat/              # AI orchestrator, agents, LLM adapters, tools
          agents/          # Agent framework (SpecializedAgent, HookManager, Registry)
            configs/       # YAML agent configs (bi-agent, pricing-agent, unified-agent)
            prompts/       # Agent system prompts (markdown)
          routing/         # Three-tier routing (RuleRouter, SemanticRouter)
          adapters/        # LLM provider adapters (Anthropic, OpenAI, Gemini)
        ingestion/         # Stripe and Shopify sync services
      workers/             # Celery app and background tasks
      mcp/                 # MCP tool server, governance, registry
        tools/             # Tool executors (SuiteQL, BigQuery, RAG, workspace)
    alembic/               # Database migrations (53 versions)
    tests/                 # pytest async test suite (2300+ tests)
      agent_benchmarks/    # BI agent vs baseline benchmark framework
      stress/              # Concurrency stress tests
  frontend/
    src/
      app/(dashboard)/     # Dashboard pages (chat, workspace, settings, analytics)
      components/          # React components
        chat/              # Message list, chart renderer, tool cards, financial reports
        workspace/         # File tree, constellation view, changeset panel
        settings/          # Connection sections (NetSuite, BigQuery), team management
        analytics/         # Saved queries, preview modal
        ui/                # shadcn/ui primitives
      hooks/               # React Query hooks
      lib/                 # API client, types, chat-stream, utilities
  suiteapp/
    src/
      FileCabinet/SuiteScripts/  # RESTlets (file cabinet, mock data)
      Objects/                    # SDF deployment descriptors
    __tests__/                    # Jest unit tests for SuiteScripts
  docker-compose.yml       # Full-stack development environment
  docker-compose.prod.yml  # Production (GCP staging)
  CLAUDE.md                # AI coding assistant instructions
```

## Development

```bash
docker compose up -d                    # Start all services
docker compose up -d --build backend    # Rebuild backend after changes

# Run tests
cd backend && python -m pytest tests/ -q

# Lint and format
cd backend && ruff check . && ruff format .

# Deploy to staging (sequential — never force-recreate)
docker buildx build --platform linux/amd64 -t ghcr.io/aideny-kr/ecom-netsuite-suites/backend:latest --push -f backend/Dockerfile backend/
ssh staging "cd /opt/ecom-netsuite && docker compose -f docker-compose.prod.yml stop beat worker && docker compose -f docker-compose.prod.yml pull backend && docker compose -f docker-compose.prod.yml up -d backend && sleep 5 && docker compose -f docker-compose.prod.yml up -d worker && sleep 3 && docker compose -f docker-compose.prod.yml up -d beat"
```

## Security

- All credentials encrypted at rest with Fernet symmetric encryption
- JWT access + refresh tokens in HttpOnly cookies
- Row-level security (RLS) enforced via PostgreSQL `SET LOCAL`
- OAuth 2.0 PKCE for NetSuite (no client secrets)
- Service account auth for BigQuery (no token refresh)
- Read-only SQL enforcement on all query tools
- BigQuery cost guardrails (max_bytes_billed)
- Audit logging on all mutations
- No secrets in git history (verified via security audit)

## License

Proprietary. All rights reserved.
