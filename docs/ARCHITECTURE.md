# Architecture
_Last updated: 2026-02-19_

## Goals
- Multi-tenant, secure, audit-friendly (finance-grade)
- Integrations with NetSuite + Shopify + Stripe
- Background jobs for sync/recon/scheduled reports
- Tool-governed AI actions (SuiteQL, exports, change requests)
- Multi-agent AI chat with RAG, web search, and external MCP tool access
- Table-first UX for visibility and operational drill-down
- Dev Workspace IDE for SuiteScript development with review workflow

## Documents
- `TENANCY_RBAC.md`
- `SECURITY.md`
- `DATA_PIPELINE_IDEMPOTENCY.md`
- `OBSERVABILITY.md`
- `AUDIT_LOGGING.md`
- `DATA_MODEL_OVERVIEW.md`

## High-Level Components

### 1) Frontend (Next.js 14 App Router)
- TypeScript strict, TanStack React Query, Tailwind CSS, shadcn/ui
- Dashboard with tables, charts, and drill-down views
- AI Chat interface (dashboard-scoped and workspace-scoped sessions)
- Dev Workspace IDE (Monaco editor, file tree, changeset review, chat panel)
- Onboarding wizard for tenant setup and NetSuite connection
- Settings: connections, AI provider (BYOK), policies, users/RBAC

### 2) API Service (FastAPI)
- Auth: JWT (access + refresh), multi-tenant, role-based permissions
- Tenant config, RBAC + entitlements, BYOK AI provider management
- Connection management (OAuth 2.0 for NetSuite, API keys for Shopify/Stripe)
- Chat endpoints (sessions, messages, streaming responses)
- Chat API keys for external integration (`/api/v1/integration/chat`)
- MCP connector registry (external MCP servers like NetSuite AI Connector)
- Workspace endpoints (files, changesets, runs, artifacts)
- Table explorer with CSV export
- Onboarding wizard (profiles, discovery, checklist, policy setup)
- NetSuite metadata discovery and SuiteScript file sync
- Audit log, job tracking, schedule management

### 3) Worker Service (Celery)
- Ingestion sync pipelines (Shopify/Stripe/NetSuite)
- NetSuite metadata discovery (custom fields, record types, subsidiaries)
- SuiteScript file sync (pull/push via File Cabinet RESTlet)
- Workspace runs (SDF validate, Jest tests, SuiteQL assertions)
- Scheduled tasks and report generation

### 4) MCP Server (Python)
- Local tool registry with governance (rate limits, timeouts, entitlements, audit)
- Tools: SuiteQL, connectivity test, metadata, report export, RAG search, web search
- Workspace tools: list_files, read_file, search, propose_patch
- External MCP connector support (discovers and proxies tools from remote MCP servers)

### 5) AI Chat System
- **Orchestrator** (`orchestrator.py`): Main agentic loop with tool execution
- **Multi-Agent Coordinator** (`coordinator.py`): Supervisor pattern — decomposes questions, delegates to specialist agents, synthesizes answers
- **Specialist Agents**:
  - **SuiteQL Agent**: Reasoning-first SQL generation with metadata tools, RAG search, and external MCP tools. Uses Claude Sonnet for strong SQL reasoning.
  - **RAG Agent**: Searches documentation (pgvector) and web (DuckDuckGo). Uses Claude Haiku for efficiency.
  - **Data Analysis Agent**: Interprets and aggregates query results.
- **BYOK Support**: Tenants can bring their own OpenAI/Anthropic API key. The coordinator uses the tenant's model for planning/synthesis, but specialists always use the platform's Anthropic key.
- **RAG Pipeline**: Markdown docs chunked and embedded via Voyage AI (1024-dim vectors), stored in `doc_chunks` with pgvector. Includes NetSuite SuiteQL reference docs.
- **Web Search**: DuckDuckGo fallback when RAG has no relevant results.

### 6) Data Stores
- PostgreSQL (system of record) + pgvector for RAG embeddings
- Redis (Celery broker + cache + rate limiting)
- Object storage (exports/evidence packs)

## Chat Session Segregation

Chat sessions are isolated by context:
- **Dashboard chat** (`session_type="chat"`, `workspace_id=NULL`): General-purpose AI assistant
- **Workspace chat** (`session_type="workspace"`, `workspace_id=<uuid>`): Scoped to a specific dev workspace with SuiteScript file context
- **Onboarding chat** (`session_type="onboarding"`): Guided setup wizard
- **External integration** (`/api/v1/integration/chat`): API-key authenticated endpoint for customer-facing chat

Sessions are filtered by context — workspace chat shows only that workspace's sessions, dashboard shows only general chats.

## NetSuite Integration

### OAuth 2.0 Authorization Code Flow (PKCE)
- Two OAuth clients: one for REST API (SuiteQL, file sync, metadata) and one for MCP (AI tools)
- Token storage: encrypted in `connections` table with Fernet symmetric encryption
- Auto-refresh via `get_valid_token()` on every API call

### SuiteScript File Sync
- Custom RESTlet (`ecom_file_cabinet_restlet.js`) for File Cabinet I/O
- RESTlet uses delete+recreate for updates (file IDs change — tracked in `workspace_files.netsuite_file_id`)
- Bidirectional sync: pull from NetSuite, push changes back

### Metadata Discovery
- Celery task discovers custom fields, record types, subsidiaries, departments, locations
- Results stored as versioned snapshots in `netsuite_metadata` table
- Injected into SuiteQL agent system prompt for accurate query generation

### NetSuite MCP Connector
- Connects to NetSuite's native AI Connector Service (`/services/mcp/v1/all`)
- Tools discovered dynamically via MCP protocol (`list_tools()`)
- Tool calls proxied with OAuth2 token refresh
- Governance layer applies same rate limits, timeouts, and audit as local tools

## Dev Workspace Runner and Testing Pipeline

### Components
- **Workspace Service (Virtual FS):** stores/imports SDF-style project snapshots, exposes file operations for IDE UI and chat references (`@workspace:/path`)
- **Change Set Service:** diff-based change sets with approval state machine (`draft → pending_review → approved → applied`)
- **Runner Service:** executes allowlisted commands in isolated per-tenant workspaces, produces immutable artifacts
- **Runs + Artifacts Model:** every validate/test/deploy/assertion operation is a Run record linked to audit events

### Privileged Operations and Gating
Privileged actions (validate/tests/deploy/apply_patch) must be:
- Tenant-isolated and RBAC protected
- Approval-gated at the Change Set level
- Fully auditable with correlation_id and artifact references

### Tooling
Runner allowlisted commands only:
- `suitecloud project:validate`
- `jest` (SuiteCloud Unit Testing)
- `suitecloud project:deploy` (sandbox only in beta)

## Tenancy Model
`tenant_id` on all rows + Postgres Row Level Security (RLS) enforced in DB and app via `SET LOCAL app.current_tenant_id`.

## AI Tool Governance
- All model actions go through the governance layer
- Tools enforce: allowlists/denylists, default LIMITs + max rows, timeouts + rate limits, mandatory audit events
- Write tools (posting) require approvals and entitlements
- Parameter allowlisting prevents injection
- Per-tool governance config in `TOOL_CONFIGS`

## Data Ingestion
- Incremental cursors per connector (stored in `cursor_states`)
- Deterministic dedupe keys for idempotent upserts
- Evidence packs for audit trail

## Observability
- Structured logs with `tenant_id` + `correlation_id`
- NetSuite API request/response logging (`netsuite_api_logs`)
- Token usage tracking per chat message (`input_tokens`, `output_tokens`, `model_used`)
- Job-level inspection and replay controls

## API Surface

| Router | Prefix | Purpose |
|--------|--------|---------|
| auth | `/api/v1/auth` | Register, login, refresh, logout, tenant switch |
| tenants | `/api/v1/tenants` | Tenant config, plan, BYOK AI settings |
| users | `/api/v1/users` | User CRUD, role assignment |
| connections | `/api/v1/connections` | Connection CRUD, test, sync trigger |
| netsuite_auth | `/api/v1/connections/netsuite` | OAuth 2.0 authorize + callback |
| tables | `/api/v1/tables` | Table explorer with CSV export |
| audit | `/api/v1/audit-events` | Paginated audit log |
| jobs | `/api/v1/jobs` | Job list and detail |
| chat | `/api/v1/chat` | Sessions, messages, health |
| chat_api_keys | `/api/v1/chat-api-keys` | External chat API key management |
| chat_integration | `/api/v1/integration/chat` | External API-key authenticated chat |
| mcp_connectors | `/api/v1/mcp-connectors` | External MCP server registry |
| workspaces | `/api/v1/workspaces` | Workspace CRUD, files, changesets, runs |
| onboarding | `/api/v1/onboarding` | Wizard, profiles, discovery, checklist |
| policies | `/api/v1/policies` | Data governance policy CRUD |
| schedules | `/api/v1/schedules` | Cron schedule management |
| netsuite_metadata | `/api/v1/netsuite/metadata` | Metadata discovery and retrieval |
| suitescript_sync | `/api/v1/netsuite/scripts` | SuiteScript file sync, mock data |
| netsuite_api_logs | `/api/v1/netsuite` | API request/response logs |
