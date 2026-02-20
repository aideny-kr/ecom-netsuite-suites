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
- **Chat**: AI orchestrator in `orchestrator.py` with `<thinking>` tags for reasoning (collapsed in UI). System prompt includes workspace tools and current file context.
- **react-resizable-panels v4**: Imports are `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`).

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
| Models | `backend/app/models/` |
| Schemas | `backend/app/schemas/` |
| Migrations | `backend/alembic/versions/` |
| Frontend pages | `frontend/src/app/(dashboard)/` |
| Frontend hooks | `frontend/src/hooks/` |
| Frontend components | `frontend/src/components/` |
| UI primitives (shadcn) | `frontend/src/components/ui/` |
| Types | `frontend/src/lib/types.ts` |
| API client | `frontend/src/lib/api-client.ts` |
| SuiteScripts | `suiteapp/src/FileCabinet/SuiteScripts/` |
| SDF Objects | `suiteapp/src/Objects/` |
| SuiteScript tests | `suiteapp/__tests__/` |
| Backend tests | `backend/tests/` |
| E2E tests | `frontend/e2e/` |
| Docs | `docs/` |

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

## Current State (update after each major change)

- **Latest migration**: 023_audit_uuidv7
- **Known gap**: OAuth reconnect just flips status, doesn't re-initiate browser flow
- **Known gap**: `inputRef` in workspace-chat-panel never attached to ChatInput
- **Deferred**: SDF CI/CD pipeline, bundle versioning strategy, RESTlet rate limiting
