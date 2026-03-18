# Ecom NetSuite Suites — Project Intelligence

> Read by Claude Code at session start. Encodes patterns, conventions, and decisions.

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
- **NetSuite Auth**: OAuth 2.0 PKCE flow. Two Integration Records per tenant: (1) REST API — scopes: RESTlets + REST Web Services + SuiteAnalytics Connect, (2) MCP — scope: NetSuite AI Connector Service only. Both Public Client, 720-hour refresh tokens. Each has its own Client ID stored per-connection — **never shared, never a global env var**. Token refresh via `get_valid_token()` (REST) and `_get_oauth2_token()` (MCP) MUST use stored per-connection `client_id`. Connections in `connections` (REST) and `mcp_connectors` (MCP) with encrypted `{access_token, refresh_token, expires_at, account_id, client_id}`.
- **Connection Setup**: REST API needs Account ID, Client ID, RESTlet URL (`metadata_json.restlet_url`). MCP needs Account ID, Client ID (separate Integration Record). Collected in onboarding (`step-connection.tsx`), editable in Settings (`netsuite-connections-section.tsx`).
- **File Cabinet I/O**: Custom RESTlet (`ecom_file_cabinet_restlet.js`) — in-place load-update-save (preserves file ID).
- **SuiteQL**: Via REST API POST `/services/rest/query/v1/suiteql` with Bearer token. Also via MCP at `/services/mcp/v1/all`.
- **Two SuiteQL paths**: Local REST API (`netsuite_suiteql` tool) supports all tables including `customrecord_*`. External MCP (`ns_runCustomSuiteQL`) works only for standard tables.
- **Chat**: Dual-path agent system. `unified_agent_enabled` per-tenant flag routes to `UnifiedAgent` or `MultiAgentCoordinator`. SSE streaming with `<thinking>` tags. See `memory/chat-architecture.md` for full details.
- **Entity Resolution**: Fast NER (Haiku) → pg_trgm fuzzy matching → `<tenant_vernacular>` XML injection. Table: `tenant_entity_mapping` with composite GIN index.
- **MCP Tools**: ~11 tools across 4 categories (Record CRUD, Reports, Saved Searches, SuiteQL). Visibility is role-permission based — see Mistakes #22. CRUD guardrails — see Mistakes #23.
- **Tool Result Interception**: `_intercept_tool_result()` in orchestrator emits SSE `data_table`/`financial_report` events, condenses results for LLM. Handles 3 formats: local SuiteQL (`columns`/`rows`), external MCP (`data` list-of-dicts), financial reports (`items`/`summary`).
- **Smart Context Injection**: `_classify_context_need()` classifies queries into 5 levels (FULL, DATA, DOCS, WORKSPACE, FINANCIAL). Falls back to FULL when uncertain.
- **Mock Data**: MockData RESTlet runs SuiteQL inside NetSuite with server-side PII masking. Never transmit real PII to our backend.
- **react-resizable-panels v4**: Imports: `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`).
- **White-Label Branding**: Per-tenant brand_name/color/logo/favicon in `tenant_configs`. `BrandingProvider` injects `--primary` CSS variable.
- **Feature Flags**: `tenant_feature_flags` table, TTL-cached. `require_feature(flag_key)` dependency returns 403 when disabled.

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

export function useResource(id: string | null) {
  return useQuery<Resource>({
    queryKey: ["resources", id],
    queryFn: () => apiClient.get<Resource>(`/api/v1/resources/${id}`),
    enabled: !!id,
  });
}

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
- Always `"use client"` at top of files using hooks
- Always use `apiClient` from `@/lib/api-client` — never raw `fetch()`
- Query keys: `["entity"]` for lists, `["entity", id]` for single, `["entity", id, "sub"]` for nested
- Mutations always invalidate parent query on success
- Use `enabled: !!id` for conditional queries

### Page Component Rules
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

## Key File Locations

| What | Where |
|------|-------|
| Chat agents | `backend/app/services/chat/agents/` |
| Chat adapters | `backend/app/services/chat/adapters/` |
| Entity resolver | `backend/app/services/chat/tenant_resolver.py` |
| Entity seeder | `backend/app/services/tenant_entity_seeder.py` |
| Types | `frontend/src/lib/types.ts` |
| API client | `frontend/src/lib/api-client.ts` |
| Settings API | `backend/app/api/v1/settings.py` |
| Feature flags | `backend/app/services/feature_flag_service.py` |
| Branding provider | `frontend/src/providers/branding-provider.tsx` |
| Knowledge crawler | `backend/app/services/knowledge/` |
| Celery tasks/Beat | `backend/app/workers/tasks/`, `backend/app/workers/celery_app.py` |
| Excel export | `backend/app/services/excel_export_service.py` |
| SuiteScripts | `suiteapp/src/FileCabinet/SuiteScripts/` |
| SDF Objects | `suiteapp/src/Objects/` |
| Specs / Plans | `docs/superpowers/specs/`, `docs/superpowers/plans/` |

## Common Mistakes to Avoid

1. **Don't use `Column()` in models** — use `mapped_column()` (SQLAlchemy 2.0)
2. **Don't use bare `Depends()`** — use `Annotated[Type, Depends()]`
3. **Don't use `PanelGroup` directly** — import `Group` aliased as `PanelGroup`
4. **Don't use `direction` prop** — it's `orientation` in react-resizable-panels v4
5. **Don't forget `await db.commit()`** after mutations
6. **Don't forget audit logging** on create/update/delete endpoints
7. **Don't use raw `fetch()`** in frontend — use `apiClient`
8. **Don't forget `"use client"`** on any file using hooks
9. **Don't use `WidthType.PERCENTAGE`** in docx — use DXA
10. **RESTlet PUT preserves file IDs** — in-place load → set `.contents` → `.save()`
11. **SuiteQL pagination** — use `FETCH FIRST N ROWS ONLY`, not `LIMIT`
12. **NetSuite account IDs** — normalize with `replace("_", "-").lower()` for URLs
13. **SuiteQL status codes** — REST API returns single-letter codes (`'B'`, `'H'`), NOT compound (`'SalesOrd:B'`). Compound codes silently fail.
14. **Agent hallucination guard** — `_task_contains_query()` in `base_agent.py` forces tool execution at step==0
15. **SET LOCAL doesn't support bind params** — use `set_tenant_context()` from `database.py` (validates UUID). Never raw f-string with user input.
16. **Redis required in production** — `token_denylist.py` and `rate_limit.py` are Redis-backed. In-memory fallback in dev only.
17. **Production secrets validated at startup** — `_validate_production_secrets()` refuses to start with default keys
18. **Swagger docs disabled in production** — `docs_url`/`redoc_url` are `None` when `APP_ENV != "development"`
19. **Migrations run in CI, not container startup** — `entrypoint.sh` doesn't run `alembic upgrade head`
20. **Two databases locally** — `.venv/bin/alembic` → Supabase (remote). Docker → `postgres:5432` (local). After adding columns, also run `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`.
21. **Alembic revision ID max 32 chars** — keep short (e.g. `039_confidence_score`)
22. **MCP tool visibility is role-permission based** — missing tools = OAuth role lacks permissions, NOT SuiteApp version. Fix: update role in NetSuite, reconnect MCP. Record Tools need `REST Web Services (Full)` + Create/Edit. Saved Search needs `Perform Search (Full)`. Administrator role CANNOT be used.
23. **MCP CRUD requires guardrails** — `ns_createRecord`/`ns_updateRecord` MUST NOT auto-execute. Always: (1) show payload, (2) get confirmation, (3) for updates show before/after via `ns_getRecord`, (4) audit log, (5) check record type allowlist.
24. **Unified agent prompt MUST stay in sync with SuiteQL agent** — both contain SuiteQL dialect rules. Copy verbatim — never paraphrase. Each rule prevents a specific production failure.
25. **External MCP response format differs** — `ns_runCustomSuiteQL` returns `{"data": [{col: val}], "queryExecuted": "...", "resultCount": N}`, NOT `{"columns": [], "rows": []}`. Test interception with both formats.
26. **Use `print(flush=True)` for docker logging** — structlog doesn't surface stdlib `logger.info` in docker logs.

## Current State

- **Latest migration**: 049_connection_alerts
- **Staging**: `api-staging.suitestudio.ai` (backend) + `staging.suitestudio.ai` (Vercel frontend). Auto-deploys from main.
- **Deploy**: Stop beat/worker first, pull, start sequentially. Never `--force-recreate` all at once — kills workers mid-refresh, consuming single-use refresh tokens.

## Known Issues

1. **OAuth scope mismatch** — REST connections lack `rest_webservices` scope. Onboarding Discovery returns 400. Chat works via MCP path.
2. **OAuth reconnect doesn't re-initiate browser flow** — just flips status. Expired refresh tokens require full re-authorization.
3. **CI failures** — `test_security_hardening.py` SSL tests fail. Not blocking.
