# Ecom NetSuite Suites — Project Intelligence

> This file is read by Claude Code at the start of every session.
> It encodes the project's patterns, conventions, and decisions so the AI doesn't have to rediscover them.

## Tech Stack

- **Frontend**: Next.js 14 (App Router), TypeScript strict, TanStack React Query, Tailwind CSS, shadcn/ui, react-resizable-panels v4
- **Backend**: FastAPI (async), SQLAlchemy 2.0 (async), Pydantic v2, Alembic, Celery + Redis
- **Database**: PostgreSQL (Supabase) with Row-Level Security
- **Auth**: JWT (access + refresh in HttpOnly cookie), multi-tenant, role-based permissions
- **Encryption**: Fernet symmetric for credentials at rest
- **Testing**: pytest (async) + Playwright E2E + Jest (@oracle/suitecloud-unit-testing for SuiteScripts)
- **SuiteApp**: SuiteScript 2.1, SDF (ACCOUNTCUSTOMIZATION), SuiteBundler for distribution

## Architecture Decisions

- **Multi-tenant**: All tables have `tenant_id`. RLS enforced via `SET LOCAL app.current_tenant_id`.
- **NetSuite Auth**: OAuth 2.0 Authorization Code flow. Token refresh handled by `get_valid_token()`.
- **File Cabinet I/O**: Custom RESTlet (`ecom_file_cabinet_restlet.js`) replaces broken REST API PATCH. RESTlet does in-place load-update-save (preserves file ID).
- **SuiteQL**: Via REST API POST `/services/rest/query/v1/suiteql` with Bearer token. Also available via MCP at `/services/mcp/v1/all`.
- **Mock Data**: MockData RESTlet runs SuiteQL inside NetSuite with server-side PII masking. Never transmit real PII to our backend.
- **Chat**: Dual-path agent system. `unified_agent_enabled` per-tenant flag routes to either (1) `UnifiedAgent` — single agent with all tools and full SuiteQL rules, or (2) `MultiAgentCoordinator` — semantic routing to specialist agents (SuiteQL, RAG, analysis, workspace). SSE streaming with `<thinking>` tags (collapsed in UI).
- **Entity Resolution**: Fast NER (Haiku) → pg_trgm fuzzy matching → `<tenant_vernacular>` XML injection into agent prompts. Table: `tenant_entity_mapping` with composite GIN index. Seeded from metadata discovery pipeline.
- **Two SuiteQL paths**: Local REST API (`netsuite_suiteql` tool) supports all tables including `customrecord_*`. External MCP (`ns_runCustomSuiteQL`) works only for standard tables. Agent prompt guides tool selection.
- **MCP Standard Tools SuiteApp**: ~11 tools across 4 categories (Record CRUD, Reports, Saved Searches, SuiteQL). Tool visibility is **role-permission based** — same SuiteApp version on two accounts can expose different tools depending on OAuth role permissions. NOT a SuiteApp version issue. After changing role permissions, must reconnect MCP to trigger `discover_tools()` refresh.
- **MCP Tool Permissions**: Record Tools require `REST Web Services (Full)` + per-record-type Create/Edit. Saved Search Tools require `Perform Search (Full)`. SuiteQL Tools require `SuiteQL` permission. Administrator role CANNOT be used — Oracle prohibits it. See `skills/netsuite-mcp/SKILL.md` for full permission matrix.
- **MCP CRUD Capability**: `ns_createRecord`, `ns_getRecord`, `ns_updateRecord`, `ns_getRecordTypeMetadata` enable the agent to CREATE and MODIFY NetSuite records (journal entries, customers, orders, etc.) — not just query. GUARDRAILS REQUIRED: always show payload + get user confirmation before create/update, log via audit_service, enforce record type allowlist per tenant.
- **react-resizable-panels v4**: Imports are `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`).
- **White-Label Branding**: Per-tenant brand_name, brand_color_hsl, brand_logo_url, brand_favicon_url in `tenant_configs`. Frontend `BrandingProvider` injects `--primary` CSS variable at runtime. Sidebar/login dynamically render tenant brand.
- **Custom Domains**: `custom_domain` + `domain_verified` on `tenant_configs`. DNS TXT verification via `domain_service.py`. Public resolver endpoint `GET /api/v1/settings/resolve-domain?domain=`.
- **Feature Flags**: `tenant_feature_flags` table with TTL-cached service (`feature_flag_service.py`). `require_feature(flag_key)` FastAPI dependency returns 403 when disabled. Default flags seeded on tenant creation.
- **Soul Seeding**: `seed_default_soul()` auto-populates soul.md with tenant-specific defaults on registration. Called from `auth_service.register_tenant()`.
- **Tool Result Interception**: `_intercept_tool_result()` in orchestrator intercepts SuiteQL/financial tool results, emits SSE `data_table` or `financial_report` events for frontend rendering (`DataFrameTable` component), and condenses the result for the LLM (strips rows, keeps columns + row_count). Handles three formats: local SuiteQL (`columns`/`rows`), external MCP (`data` list-of-dicts with `queryExecuted`/`resultCount`), and financial reports (`items`/`summary`).

## Backend Patterns — FOLLOW EXACTLY

### API Endpoint
```python
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.user import User
from app.services import audit_service

router = APIRouter(prefix="/resource", tags=["resource"])

@router.post("", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
async def create_resource(
    request: ResourceCreate,
    user: Annotated[User, Depends(require_permission("resource.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        resource = await resource_service.create(db=db, tenant_id=user.tenant_id, ...)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db, tenant_id=user.tenant_id, category="resource",
        action="resource.create", actor_id=user.id,
        resource_type="resource", resource_id=str(resource.id),
    )
    await db.commit()
    await db.refresh(resource)
    return ResourceResponse(...)
```

**Rules:**
- Always use `Annotated[Type, Depends(...)]` — never bare `Depends()`
- Always audit mutations via `audit_service.log_event()`
- Always `await db.commit()` after mutations
- Error handling: catch specific exceptions → `HTTPException`
- Use `require_permission("scope.action")` for protected endpoints
- Use `get_current_user` for auth-only (no permission check)
- Register routers in `app/api/v1/router.py`

### Pydantic Schema
```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal

class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["type_a", "type_b"]

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        # validation logic
        return v

class ResourceResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    created_at: datetime
    model_config = {"from_attributes": True}
```

### SQLAlchemy Model
```python
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class Resource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "resources"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Always use Mapped[] + mapped_column() — never Column()
```

### Alembic Migration
```python
"""NNN_description.py"""
from alembic import op
import sqlalchemy as sa

revision = "NNN"
down_revision = "previous"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("table", sa.Column("field", sa.String(50), nullable=True))

def downgrade() -> None:
    op.drop_column("table", "field")
```

## Frontend Patterns — FOLLOW EXACTLY

### React Query Hook
```typescript
"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

// Read hook — always conditional with enabled
export function useResource(id: string | null) {
  return useQuery<Resource>({
    queryKey: ["resources", id],
    queryFn: () => apiClient.get<Resource>(`/api/v1/resources/${id}`),
    enabled: !!id,
  });
}

// Mutation hook — always invalidate related queries
export function useCreateResource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateResourcePayload) =>
      apiClient.post<Resource>("/api/v1/resources", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}
```

**Rules:**
- Always `"use client"` at top
- Always use `apiClient` from `@/lib/api-client` — never raw `fetch()`
- Query keys: `["entity"]` for lists, `["entity", id]` for single, `["entity", id, "sub"]` for nested
- Mutations always invalidate parent query on success
- Use `enabled: !!id` for conditional queries
- Use `keepPreviousData` (import as `placeholderData`) for paginated queries

### Page Component
```typescript
"use client";
import { useAuth } from "@/providers/auth-provider";
import { SomeIcon } from "lucide-react";

export default function ResourcePage() {
  const { user } = useAuth();
  // hooks, state, handlers
  return (
    <div className="space-y-8 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-foreground">Title</h2>
        <p className="mt-1 text-[15px] text-muted-foreground">Subtitle</p>
      </div>
      {/* Content */}
    </div>
  );
}
```

**Rules:**
- Icons from `lucide-react` only
- Spacing: `space-y-8` for page sections, `gap-4` for grids
- Text sizes: `text-2xl` for page titles, `text-[15px]` for body, `text-[13px]` for labels/captions
- Colors: `text-foreground` for primary, `text-muted-foreground` for secondary
- Cards: `rounded-xl border bg-card p-5 shadow-soft`
- Use `animate-fade-in` on page root

## SuiteScript Patterns

### RESTlet
```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['N/file', 'N/log', 'N/runtime', 'N/error'], (file, log, runtime, error) => {
    const get = (requestParams) => {
        try {
            const script = runtime.getCurrentScript();
            log.debug('Operation', JSON.stringify(requestParams));
            // ... logic
            return { success: true, data: result, remainingUsage: script.getRemainingUsage() };
        } catch (e) {
            log.error('Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };
    return { get };
});
```

**Rules:**
- Always return `{ success: true/false }` envelope
- Always log with `N/log` (debug for info, audit for mutations, error for failures)
- Always report `remainingUsage` for governance monitoring
- Always wrap in try/catch — RESTlets must not throw unhandled errors

## File Locations

| What | Where |
|------|-------|
| API routes | `backend/app/api/v1/` |
| Services | `backend/app/services/` |
| Chat agents | `backend/app/services/chat/agents/` |
| Chat adapters | `backend/app/services/chat/adapters/` |
| Entity resolver | `backend/app/services/chat/tenant_resolver.py` |
| Entity seeder | `backend/app/services/tenant_entity_seeder.py` |
| Models | `backend/app/models/` |
| Schemas | `backend/app/schemas/` |
| Migrations | `backend/alembic/versions/` |
| Frontend pages | `frontend/src/app/(dashboard)/` |
| Frontend hooks | `frontend/src/hooks/` |
| Frontend components | `frontend/src/components/` |
| UI primitives (shadcn) | `frontend/src/components/ui/` |
| Types | `frontend/src/lib/types.ts` |
| API client | `frontend/src/lib/api-client.ts` |
| Settings API | `backend/app/api/v1/settings.py` |
| Feature flags | `backend/app/services/feature_flag_service.py` |
| Domain service | `backend/app/services/domain_service.py` |
| Branding provider | `frontend/src/providers/branding-provider.tsx` |
| Feature hooks | `frontend/src/hooks/use-features.ts` |
| Knowledge crawler | `backend/app/services/knowledge/` |
| Celery tasks | `backend/app/workers/tasks/` |
| Celery Beat config | `backend/app/workers/celery_app.py` |
| Invite service | `backend/app/services/invite_service.py` |
| Google auth | `backend/app/services/google_auth_service.py` |
| Excel export | `backend/app/services/excel_export_service.py` |
| Export endpoints | `backend/app/api/v1/exports.py` |
| Invite endpoints | `backend/app/api/v1/invites.py` |
| SuiteScripts | `suiteapp/src/FileCabinet/SuiteScripts/` |
| SDF Objects | `suiteapp/src/Objects/` |
| SuiteScript tests | `suiteapp/__tests__/` |
| Backend tests | `backend/tests/` |
| E2E tests | `frontend/e2e/` |
| Docs | `docs/` |
| Specs | `docs/superpowers/specs/` |
| Plans | `docs/superpowers/plans/` |

## Common Mistakes to Avoid

1. **Don't use `Column()` in models** — use `mapped_column()` (SQLAlchemy 2.0 style)
2. **Don't use bare `Depends()`** — use `Annotated[Type, Depends()]`
3. **Don't use `PanelGroup` from react-resizable-panels** — it's `Group` (aliased as `PanelGroup`)
4. **Don't use `direction` prop** — it's `orientation` in react-resizable-panels v4
5. **Don't forget `await db.commit()`** after mutations
6. **Don't forget audit logging** on create/update/delete endpoints
7. **Don't use raw `fetch()`** in frontend — use `apiClient`
8. **Don't forget `"use client"`** on any file using hooks
9. **Don't use `WidthType.PERCENTAGE`** in docx — use DXA
10. **RESTlet PUT preserves file IDs** — in-place update via load → set `.contents` → `.save()`
11. **SuiteQL pagination** — use `FETCH FIRST N ROWS ONLY`, not `LIMIT` (not supported in SuiteQL)
12. **NetSuite account IDs** — normalize with `replace("_", "-").lower()` for URLs
13. **SuiteQL status codes via REST API** — REST API returns single-letter status codes (`'B'`, `'H'`), NOT compound codes (`'SalesOrd:B'`). Always use single-letter codes in WHERE filters: `t.status NOT IN ('C', 'H')`. Compound codes silently fail via REST API.
14. **Agent hallucination from history** — The LLM may answer data queries from conversation memory without calling tools. `_task_contains_query()` guard in `base_agent.py` forces tool execution when step==0 and no tools called.
15. **SET LOCAL doesn't support bind params** — PostgreSQL `SET LOCAL` rejects `$1` placeholders. Use `set_tenant_context()` from `database.py` which validates UUID before interpolation. Never use raw f-string with user input.
16. **Token denylist and rate limiter use Redis** — `token_denylist.py` and `rate_limit.py` are Redis-backed. Falls back to in-memory in dev if Redis unavailable. Must have Redis in production.
17. **Production secrets validated at startup** — `_validate_production_secrets()` in `main.py` refuses to start if `APP_ENV != "development"` and JWT_SECRET_KEY or ENCRYPTION_KEY are defaults.
18. **Swagger docs disabled in production** — `docs_url` and `redoc_url` are `None` when `APP_ENV != "development"`.
19. **Migrations run in CI, not container startup** — `entrypoint.sh` no longer runs `alembic upgrade head`. Run migrations via deploy.yml workflow.
20. **Two databases locally** — `.venv/bin/alembic` runs against Supabase (remote). Docker containers use `postgres:5432` (local). After adding a model column, run `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head` to migrate the local Docker Postgres too, or the backend will crash with `UndefinedColumnError`.
21. **Alembic revision ID max 32 chars** — `alembic_version.version_num` is `VARCHAR(32)`. Keep revision IDs short (e.g. `039_confidence_score`, not `039_chat_message_confidence_score`).
22. **MCP tool visibility is role-permission based** — If a tenant is missing MCP tools (e.g., no Record Tools, no Saved Search Tools), it's because their OAuth role lacks the required permissions — NOT a SuiteApp version issue. Fix: update the role in NetSuite (Setup > Users/Roles > Manage Roles), then reconnect MCP. Record Tools need `REST Web Services (Full)` + record-type Create/Edit. Saved Search Tools need `Perform Search (Full)`.
23. **MCP CRUD requires guardrails** — `ns_createRecord` and `ns_updateRecord` MUST NOT auto-execute. Always: (1) show the full payload to the user, (2) get explicit confirmation, (3) for updates, show before/after diff via `ns_getRecord` first, (4) log via `audit_service`, (5) check record type allowlist. The agent is now an action agent, not just read-only.
24. **Unified agent prompt MUST stay in sync with SuiteQL agent** — `unified_agent.py` and `suiteql_agent.py` both contain SuiteQL dialect rules. When adding a new rule to one, add it to both. Copy rules verbatim — do NOT paraphrase or "simplify" when porting. Each rule was added because of a specific production failure; losing details causes regressions (e.g., missing `assemblycomponent = 'F'` caused double-counting).
25. **External MCP response format differs from local** — `ns_runCustomSuiteQL` returns `{"data": [{col: val}, ...], "queryExecuted": "...", "resultCount": N}`, NOT `{"columns": [], "rows": []}`. The `_intercept_tool_result()` function handles both formats. When adding new interception logic, test with both local and external MCP tool names.

## Chat Architecture

- **Orchestrator** (`orchestrator.py`): SSE streaming endpoint. Checks `TenantConfig.unified_agent_enabled` to route to `UnifiedAgent` or `MultiAgentCoordinator`.
- **Unified Agent** (`unified_agent.py`): Single agent with all tools (SuiteQL, RAG, workspace, financial). Used by Framework tenant. Has full SuiteQL dialect rules + `<common_queries>` section embedded in its system prompt.
- **Coordinator** (`coordinator.py`): Legacy multi-agent path. Semantic router with heuristic classifier (`classify_intent()`), LLM fallback for ambiguous queries. Dispatches specialist agents, handles retries, streams synthesis. Used by Rails tenant.
- **Intent types**: DOCUMENTATION, DATA_QUERY, FINANCIAL_REPORT, CODE_UNDERSTANDING, WORKSPACE_DEV, ANALYSIS, AMBIGUOUS. Heuristic regex rules checked first; AMBIGUOUS falls back to LLM planner.
- **Financial report routing**: FINANCIAL_REPORT intent → local `netsuite.financial_report` tool (SuiteQL-first with BUILTIN.CONSOLIDATE for posting-time FX rates). MCP `ns_runReport` as fallback only (uses real-time FX, diverges on multi-currency tenants). Server-side `_compute_summary()` pre-computes section totals — single-period returns flat dict, trend returns `summary.by_period` keyed by periodname. LLM presents numbers, never computes. Financial reports bypass `NETSUITE_SUITEQL_MAX_ROWS` cap via `_skip_limit_cap` kwarg.
- **Hybrid MCP architecture**: Three layers — (1) Context Layer: entity resolution, tenant schema, RAG, learned rules, proven patterns (always active), (2) Execution Layer: local `netsuite.financial_report` for P&L/BS/TB (verified templates), MCP tools for discovery/saved searches/ad-hoc (`ns_runSavedSearch` → `ns_runCustomSuiteQL`), local `netsuite_suiteql` as fallback, (3) Knowledge Layer: `rag_search` + `web_search`.
- **MCP tool detection**: Orchestrator uses `_MCP_TOOL_PATTERNS` dict to detect all ~11 tool types from `ext__` prefixed names. Patterns: runreport, runsavedsearch, listallreports, listsavedsearches, suiteql, getsuiteqlmetadata, getsubsidiaries, createrecord, getrecord, updaterecord, getrecordtypemetadata.
- **Specialist agents** (`agents/`): Used by multi-agent coordinator path only. Each runs a mini agentic loop with tools (max_steps varies per agent)
  - `SuiteQLAgent`: max_steps=6, has comprehensive SuiteQL rules (the canonical source — unified agent rules must mirror these)
  - `RAGAgent`: max_steps=2, strict tool budget (2 rag_search + 1 web_search). Handles docs, script logic, AND online research.
  - `DataAnalysisAgent`: requires prior data from SuiteQL agent
  - `WorkspaceAgent`: file ops, propose_patch, search workspace
  - `UnifiedAgent`: Single agent with all capabilities. Replaces coordinator routing for `unified_agent_enabled=True` tenants.
- **LLM adapters** (`adapters/`): Anthropic, OpenAI, Gemini — all implement `create_message()` and `stream_message()`
- **Entity resolution** (`tenant_resolver.py`): Runs before SuiteQL agent dispatch. Haiku extracts entities → pg_trgm resolves to script IDs → XML block injected via `context["tenant_vernacular"]`
- **Route registry**: Add new agents via `ROUTE_REGISTRY` dict + `_create_agent()` factory in coordinator
- **History compaction**: Per-message `content_summary` generated at write-time (Haiku). Orchestrator loads summaries for older messages, full content for last 8. No read-time LLM compaction.
- **Session ordering**: Sorted by `updated_at DESC`. Session `updated_at` bumped on every message. Frontend auto-selects most recent session on page load.
- **Doc chunk embeddings**: OpenAI `text-embedding-3-small` (1024-dim) primary, Voyage AI fallback. 3,198 chunks embedded. UTF-8 sanitized on ingest.
- **Logging**: Coordinator/orchestrator use `print(flush=True)` for docker visibility (structlog doesn't surface stdlib `logger.info` calls).

## Current State (updated 2026-03-17)

- **Latest migration**: 048_onboarding_profile
- **Staging**: `staging.suitestudio.ai` (Vercel frontend) + `api-staging.suitestudio.ai` (GCP VM backend). Auto-deploys from main via GitHub Actions. SSH deploy key configured.
- **RBAC**: 3 user-facing roles (Admin/Finance/Operations). `chat.financial_reports` permission gates financial reports. Invite flow with email (console/Resend) + Google Sign-In.
- **Google Sign-In**: `@react-oauth/google` on login + invite pages. Backend `POST /auth/google` verifies ID token, auto-links Google account. Client ID: `840124956248-*.apps.googleusercontent.com` (suite-studio-ai project).
- **BYOK**: Tenants with `ai_api_key_encrypted` use their own provider + model for the unified agent. Non-BYOK tenants use platform defaults. Credits not deducted for BYOK.
- **Excel export**: `POST /exports/excel` (direct data), `POST /exports/query-export` (server-side re-execution with pagination). openpyxl with auto-type detection, branded styling. `apiClient.download()` for blob responses.
- **Structured output persistence**: `structured_output` JSONB on `chat_messages` persists financial reports and data tables across page refresh. Frontend hydrates refs on session load.
- **Saved queries**: Private by default (`created_by` + `is_public`). Publish/unpublish via `PATCH /skills/{id}/publish`. Snapshot data stored in `result_data` JSONB for financial reports.
- **Connection management**: Grouped "NetSuite Connections" section in Settings. Per-connection health check (`GET /connections/health`), editable Client IDs, RESTlet URL. Token expiry detection flips status to `needs_reauth`.
- **Knowledge Crawler**: 4 sources (Oracle Help, Tim Dietrich, SuiteRep, Reddit r/Netsuite). Daily at 3am UTC via Celery Beat. Incremental (content hash dedup). 108+ crawled chunks embedded.
- **Auto-Learning**: Gap detector (thumbs-down + tool errors) → web search → chunk → embed. Daily at 4am UTC. 30-day staleness check refreshes tenant onboarding profiles.
- **Onboarding Discovery**: 6-phase tenant deep discovery (transaction landscape, relationships, status codes). Stores `onboarding_profile` JSON in `tenant_configs`. Injected into agent prompt as XML blocks. Known: needs `rest_webservices` OAuth scope to work.
- **Golden dataset**: 10 files, 89 chunks (added `transaction-relationships.md` for createdfrom, transaction chains, RMA patterns).
- **Settings visibility**: Non-admin users see My Account + Connection Status only. Admins see full management (Team, Connections, Jobs, AI, Branding, etc.).
- **Dark mode**: Default theme.

## Known Issues

1. **OAuth scope mismatch** — REST API connections lack `rest_webservices` scope, so Onboarding Discovery queries return 400. Chat agent works fine via MCP path. Fix: re-auth connections with proper scope, or wire discovery to use MCP tools.
2. **OAuth reconnect doesn't re-initiate browser flow** — just flips status. Expired refresh tokens (e.g., Rails 9745435) require full re-authorization.
3. **structlog doesn't surface stdlib logging** — `logging.getLogger()` calls don't appear in docker logs. Use `print(flush=True)` for visibility.
4. **Token storage duplication** — `localStorage.setItem` + `document.cookie` pattern in 3 places (login, invite, Google). Should use shared `setTokens()` from auth-provider.
5. **Onboarding profile XML duplicated** — identical 50-line builder in orchestrator.py and coordinator.py. Should extract to shared `context_builders.py`.
6. **SYSTEM_TENANT_ID constant scattered** — defined in 5+ files. Should consolidate to `app/core/constants.py`.
7. **Celery async boilerplate** — `asyncio.new_event_loop()` pattern repeated in 8+ tasks. Should extract `run_async()` helper.
8. **Team section MAX_SEATS hardcoded** — Currently `20`, should come from entitlement API.
9. **CI failures** — `test_security_hardening.py` SSL tests fail in CI. Not blocking but noisy.
10. **OAuth/MCP tokens expire frequently** — Both REST API and MCP OAuth tokens expire and don't auto-refresh reliably. `get_valid_token()` refreshes 60s before expiry, but if the refresh token itself expires (NetSuite default: 7 days), the connection goes dead silently. Symptoms: chat queries fail, health check shows `needs_reauth`. No proactive notification to admin. Fix needed: (a) background token refresh job that runs before expiry, (b) alert/notification when a connection goes stale, (c) consider longer-lived refresh tokens via NetSuite integration record settings.

## Roadmap

### Go-live blockers
- [ ] Resend email provider for production invite emails
- [ ] Production deployment (Supabase, Caddy, `suitestudio.ai` domain)
- [ ] Billing/payment gateway (Stripe — model TBD)
- [ ] Fix OAuth scope for discovery (re-auth with `rest_webservices` or wire MCP path)
- [ ] Google OAuth consent verification (remove "unverified app" warning)
- [ ] **Proactive token refresh** — background Celery job that refreshes OAuth tokens before expiry + alerts admin when refresh fails

### Short-term (quality + UX)
- [ ] Settings: read-only team list for non-admins
- [ ] Onboarding Discovery via MCP (bypass scope issue)
- [ ] Refactor token storage (shared `setTokens()`)
- [ ] Extract shared context builders (profile XML, domain knowledge)
- [ ] Celery `run_async()` helper to eliminate boilerplate
- [ ] Knowledge Crawler: admin-configurable custom sources

### Medium-term (features)
- [ ] Financial DataFrame component (`<FinancialReport />` with section grouping)
- [ ] Onboarding Discovery Phase 3/5/6 (custom fields, sample queries, saved searches)
- [ ] Auto-trigger discovery on new OAuth connection
- [ ] Celigo integration (research complete, see `memory/celigo-research.md`)
- [ ] SDF CI/CD pipeline for SuiteScript deployment
- [ ] SYSTEM_TENANT_ID consolidation to `app/core/constants.py`
