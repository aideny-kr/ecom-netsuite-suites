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
- **Chat**: Unified agent system. `unified_agent_enabled` per-tenant flag routes to `UnifiedAgent`. SSE streaming with `<thinking>` tags. Connection-aware orchestrator checks REST/MCP health pre-flight and strips tools for dead connections. See `memory/chat-architecture.md` for full details.
- **Agent Framework (v1.1)**: Composition + hooks architecture for specialized agents. `SpecializedAgent` extends `BaseSpecialistAgent` via YAML config + prompt files + `HookManager`. Three-tier routing: (1) regex rule-based < 1ms, (2) Haiku semantic classification ~50ms, (3) UnifiedAgent fallback. Agent Registry manages lifecycle, health checks (circuit breaker at 5% error rate), and per-tenant overrides via `agent_configs` table. RAG partitions isolate knowledge per agent. Every specialized agent must beat native Claude + MCP on its domain. See `memory/agent-framework.md` for full architecture and `docs/superpowers/specs/2026-03-22-custom-agent-architecture.md` for implementation plan.
- **BigQuery BI Agent (v1.1)**: First specialized agent — natural language → BigQuery SQL → charts. Service account auth (not OAuth). Connector stored in `mcp_connectors` with `provider: "bigquery"`. Tools: `bigquery_sql` (read-only, cost-guardrailed), `bigquery_schema` (discover tables/columns), `bigquery_cost_estimate` (dry-run). Chart specs emitted via `<chart>` XML tags in agent response, extracted by orchestrator into `chart` SSE events. Frontend renders with recharts. Schema auto-seeded into RAG partitions on connection. See `docs/superpowers/specs/2026-03-22-bigquery-bi-agent.md`.
- **Entity Resolution**: Fast NER (Haiku) → pg_trgm fuzzy matching (threshold `_MIN_ENTITY_CONFIDENCE = 0.70`) → `<tenant_vernacular>` XML injection. Table: `tenant_entity_mapping` with composite GIN index. Matches below 0.70 are skipped to prevent wrong field injection.
- **MCP Tools**: ~11 tools across 4 categories (Record CRUD, Reports, Saved Searches, SuiteQL). Visibility is role-permission based — see Mistakes #22. CRUD guardrails — see Mistakes #23.
- **Tool Result Interception**: `_intercept_tool_result()` in orchestrator emits SSE `data_table`/`financial_report` events, condenses results for LLM. Handles 3 formats: local SuiteQL (`columns`/`rows`), external MCP (`data` list-of-dicts), financial reports (`items`/`summary`).
- **Smart Context Injection**: `_classify_context_need()` classifies queries into 5 levels (FULL, DATA, DOCS, WORKSPACE, FINANCIAL). Falls back to FULL when uncertain.
- **Investigation Mode**: When `context_need == FULL`, conditional guards activate: 12-step budget (vs 6), no early exit, no data nudge, progressive output (replaces "ONLY ONE sentence"), systemnote expertise appended. `_INVESTIGATION_RE` regex classifies history/timeline/audit/why queries as FULL. Outperforms Claude + native MCP on investigation queries.
- **Pivot Tool**: `pivot.query_result` (LLM sees `pivot_query_result`) — server-side deterministic pivoting for both SuiteQL and BigQuery. Agent runs flat GROUP BY, then calls pivot tool which re-executes without row limit and pivots in Python. Auto-detects dialect from backtick identifiers. Natural sort for pivot columns (M+1, M+2, ..., M+10 not M+1, M+10, M+2). Do NOT build CASE WHEN pivot SQL manually.
- **History Condensation**: `build_condensed_history()` replaces large JSON blocks in older messages with summaries. Last 4 messages kept verbatim. Reduces follow-up tokens from ~100K to ~40K.
- **Haiku Routing**: Simple lookups (single entity, simple counts) route to `claude-haiku-4-5-20251001` for 10x speed. Only for non-BYOK tenants. Conservative regex — FULL model when uncertain.
- **Mock Data**: MockData RESTlet runs SuiteQL inside NetSuite with server-side PII masking. Never transmit real PII to our backend.
- **react-resizable-panels v4**: Imports: `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`).
- **White-Label Branding**: Per-tenant brand_name/color/logo/favicon in `tenant_configs`. `BrandingProvider` injects `--primary` CSS variable.
- **Feature Flags**: `tenant_feature_flags` table, TTL-cached. `require_feature(flag_key)` dependency returns 403 when disabled.
- **Write-Back Confirmation**: All mutation-path agents use shared confirmation flow: agent builds payload → SSE `confirmation_required` event → frontend `ConfirmationDialog` component → user approves → agent executes → audit log with before/after snapshots. Never auto-execute writes.

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
| Pivot service | `backend/app/services/pivot_service.py` |
| Redis lock | `backend/app/core/redis_lock.py` |
| Proactive refresh | `backend/app/workers/tasks/proactive_token_refresh.py` |
| Connection alerts | `backend/app/api/v1/connection_alerts.py` |
| SuiteScripts | `suiteapp/src/FileCabinet/SuiteScripts/` |
| SDF Objects | `suiteapp/src/Objects/` |
| Agent configs (YAML) | `backend/app/services/chat/agents/configs/` |
| Agent prompts | `backend/app/services/chat/agents/prompts/` |
| Agent registry | `backend/app/services/chat/agents/agent_registry.py` |
| Agent routing | `backend/app/services/chat/routing/` |
| Agent benchmarks | `backend/tests/agent_benchmarks/` |
| BigQuery service | `backend/app/services/bigquery_service.py` |
| BigQuery tools | `backend/app/mcp/tools/bigquery_tools.py` |
| BigQuery schema seeder | `backend/app/services/bigquery_schema_seeder.py` |
| Chart extractor | `backend/app/services/chat/chart_extractor.py` |
| Chart renderer | `frontend/src/components/chat/chart-renderer.tsx` |
| Specs / Plans | `docs/superpowers/specs/`, `docs/superpowers/plans/` |
| Architecture memory | `memory/` |

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
13. **SuiteQL status codes** — REST API returns single-letter codes (`'B'`, `'H'`), NOT compound (`'SalesOrd:B'`). Compound codes silently fail. RMA received = `status IN ('D','E','F','G','H')`. See `knowledge/golden_dataset/transaction-types-and-statuses.md` for all types.
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
27. **Agent framework uses composition, not inheritance** — `SpecializedAgent` is driven by YAML config + hooks. Do NOT create deep inheritance chains. Each agent is a config file + prompt file + optional hook functions.
28. **Three-tier routing order matters** — Tier 1 (regex) → Tier 2 (Haiku semantic) → Tier 3 (UnifiedAgent fallback). Never skip tiers. Ambiguous Tier 1 matches (2+ agents) escalate to Tier 2, not pick first.
29. **Agent tool filtering is per-agent** — each agent's YAML `tools:` list controls visibility. Don't expose all tools to all agents. Use `get_tools_for_agent(agent_id)`.
30. **RAG partitions are per-agent** — add `partition_id` filter when querying `domain_knowledge_chunks`. Never mix partitions across agents.
31. **Every specialized agent benchmarks against native Claude + MCP** — pass@5 consistency test, not single-run. Circuit breaker auto-disables at 5% error rate over last 100 queries.
32. **BigQuery tool names use dots in registry but underscores in LLM** — `bigquery.sql` in tool registry becomes `bigquery_sql` for the LLM. The name sanitizer handles this automatically.
33. **BigQuery uses LIMIT not FETCH FIRST** — `FETCH FIRST N ROWS ONLY` is SuiteQL syntax. BigQuery Standard SQL uses `LIMIT N`.
34. **Chart extraction happens post-stream** — same pattern as confidence tag extraction. `extract_charts()` runs after full response, emits `chart` SSE events.

## Current State

- **Product**: AI-den v1.1 deployed to staging 2026-03-23. Agent framework + BigQuery BI agent + chart rendering all live.
- **Roadmap**: v1.1 shipped (agent framework + BigQuery BI) → v1.2 Early May (NetSuite read-write, ~2wk) → v1.3 Late May (cross-system intelligence, ~3wk) → v1.4 Mid-Jun (ETL pipelines, ~3wk).
- **Latest migration**: 055_eval_cases
- **CI status**: Lint + format passing. Backend tests passing (pre-existing SSL test excluded).
- **Staging**: `api-staging.suitestudio.ai` (backend). Deploy path: `/opt/ecom-netsuite` with `docker-compose.prod.yml`.
- **Deploy**: Stop beat/worker first, pull, start sequentially. Never `--force-recreate` all at once — kills workers mid-refresh, consuming single-use refresh tokens.

## Known Issues

1. **CI failures** — `test_security_hardening.py` SSL tests fail. Not blocking deploy (manual `workflow_dispatch` bypasses CI gate).
2. **Workspace RAG chunking** — preamble code (constants before first entry point) now captured as `#preamble` chunk. Force re-seed after changing chunking logic: `seed_workspace_scripts(db, tenant_id, force=True)`.
3. **CSV/Excel export** — strips `FETCH FIRST N ROWS ONLY` before re-executing server-side (up to 50K rows). Filenames are `query-results-YYYY-MM-DD`.
4. **LLM pivot limitation** — the LLM cannot reliably build CASE WHEN pivot SQL (drops variants, adds non-existent values). Always use `pivot_query_result` tool instead. Prompt says this but LLM occasionally ignores it.
5. **Proven patterns can poison** — bad query patterns auto-saved from failed attempts get injected into follow-up queries. If the agent produces consistently wrong queries, check `tenant_query_patterns` table and delete bad patterns.

## Resolved (2026-03-18)

- **Token refresh** — per-connection client_id, immediate commit per-connection, Redis lock, proactive 5-min task. `expires_in` cast to int (NetSuite returns string). Confirmed self-sustaining on both staging and local.
- **OAuth re-auth** — upsert matches any non-revoked connection (was only matching `active`, creating duplicates). Resets status + error_reason.
- **Entity seeder** — now seeds locations, subsidiaries, departments, classes from metadata (was missing, causing "Panurgy" to match custom fields).
- **RMA status codes** — added to static prompt + golden dataset. Received = `D,E,F,G,H`. Status codes discovered from live NetSuite REST API.
- **History summaries** — backfilled 204 messages. New messages auto-summarize at write-time (Haiku).
- **10x agent quality** (PRs #16-20) — connection-aware orchestrator, entity resolver 0.70 threshold, 7-step workflow with anti-enrichment, programmatic stop-when-done, early exit preserves knowledge tools, RAG keyword boosting + H2 titles, preamble chunking, workspace ID routing, change request dedup. Cost per query: $2.29 → $0.29 (87% reduction).

## Resolved (2026-03-19)

- **Pivot queries** — new `pivot_query_result` tool for deterministic server-side pivoting. No more LLM-built CASE WHEN SQL.
- **Token condensation** — `build_condensed_history()` reduces follow-up tokens from ~100K to ~40K.
- **Haiku routing** — simple lookups route to Haiku for 10x speed (non-BYOK only).
- **Saved query CSV export** — was downloading stale files from disk (wiped by Docker restarts). Now re-executes query on demand.

## Resolved (2026-03-21)

- **Investigation mode** — conditional on `context_need == FULL`: expanded `_INVESTIGATION_RE` regex (history/timeline/audit trail/what happened/how long/when was), 12-step budget, disabled early exit + data nudge, progressive output instructions (replaces one-sentence constraint), systemnote expertise block (`recordtypeid = -30`, raw field names, context codes). 3 files changed (~30 lines), 12 new tests. Beats Claude + native MCP on R850152063 benchmark.

## Resolved (2026-03-22)

- **Streaming markdown jump** — replaced `<pre>` with memoized `StreamingMarkdown` component during streaming. Uses `React.memo` with 50-char threshold to batch visual updates. No more reformatting jump when stream completes.
- **Confidence miscalibration** — `strip_confidence_tag()` was removing the agent's `<confidence>N</confidence>` BEFORE `extract_structured_confidence()` could read it, forcing Haiku fallback with a generic rubric every time. Fix: extract → strip → display. Agent self-scores now respected; Haiku only fires when tag missing.
- **Duration precision** — added exact timestamp calculation hint to `_SYSTEMNOTE_EXPERTISE`. Agent now computes "22 hours 14 minutes" instead of "~1 day".
- **Cloudflare SSE buffering** — removed `no-chunked-encoding: true` from `/etc/cloudflared/config.yml` on staging VM. SSE now streams progressively instead of snapshotting.
- **Chat scroll jump** — wrapped `scrollIntoView` in `requestAnimationFrame` to wait for DOM layout to settle before scrolling. Fixes "message appears at bottom then shoots up".

## Resolved (2026-03-23)

- **v1.1 Agent Framework** — Full Week 1 shipped: AgentProtocol, SpecializedAgent (composition-based), HookManager, AgentYAMLConfig, AgentRegistry, three-tier routing (RuleRouter + SemanticRouter + UnifiedAgent fallback), tool filtering, RAG partitions. 131 new tests.
- **v1.1 BigQuery BI Agent** — Full Week 2 shipped: BigQuery service (read-only, cost-guardrailed), 3 tools (sql/schema/cost_estimate), connector API + frontend settings UI, BI agent YAML config + prompt, schema RAG seeder, table selector with search. 70+ BigQuery tests.
- **Chart pipeline** — `<chart>` XML extraction from agent text, SSE chart events, recharts frontend renderer (bar/line/pie/area/scatter), premium UX (smart $M formatting, gradients, glassmorphism). Chart suppression during streaming.
- **Financial report auto-charts** — Deterministic post-processing (no LLM): income statement trend → grouped bar (revenue/COGS/opex/net income), balance sheet trend → grouped bar (assets/liabilities/equity). Only for 2+ period reports.
- **Financial routing fix** — `_select_agent(is_financial=True)` bypasses agent routing for financial statements, forcing UnifiedAgent with NetSuite tools. Prevents income statement going to BigQuery BI agent.
- **Scalability Sprint 0** — DB pool 5→20 + overflow 30, gunicorn 4 workers, `/health/detailed` endpoint with pool stats + SSE counter. Stress tested to 25 concurrent chats at 100% success.
- **BigQuery connector encryption fix** — Staging connector created with wrong Fernet key. Re-encrypted with staging `ENCRYPTION_KEY`.
- **BigQuery `asyncio.to_thread()`** — All 4 service functions wrapped to prevent event loop blocking. Schema endpoint returns 502 with message on failure instead of 503 timeout.
- **BigQuery location parameter** — `location` field threaded through entire stack (schema, service, API, tools, frontend). Defaults to "US" for backward compat. Framework uses `us-central1`.
- **Saved search interception** — `ns_runSavedSearch` results now intercepted as `data_table` SSE events (was falling through to raw LLM response). Client-side CSV export for saved searches.
- **CSV export on all data tables** — Export CSV button added to `suiteql-tool-card.tsx`. Save to Analytics fixed for saved searches (uses `Saved Search: {id}` as query_text).
- **Saved query export fix** — Excel/CSV export on `/queries` page for saved search results uses stored snapshot instead of trying to re-execute non-SQL query text.
- **Chart persistence** — Charts stored in `structured_output.charts` array, survive page refresh. Deduplication prevents double rendering.
- **Streaming smoothness** — StreamingMarkdown memo threshold 50→15 chars, scroll debounce 50→16ms.
- **CI cleanup** — All 229 ruff lint errors resolved. 15 pre-existing test failures fixed. Migration 053 added for missing `use_mcp_financial_reports` column.
- **Invite role label** — "User" → "Finance" in team invite dropdown.
- **Email config** — Staging configured with Resend provider for invite emails.

## Resolved (2026-03-25)

- **Pivot tool renamed** — `netsuite.pivot_query_result` → `pivot.query_result`. Now supports both SuiteQL and BigQuery dialects with auto-detection (backtick identifiers → BigQuery). Updated across ~20 files.
- **Pivot column natural sort** — pivot columns now sorted numerically (M+1, M+2, ..., M+10) instead of lexicographically (M+1, M+10, M+11, M+2). Uses `_natural_sort_key()` with regex digit splitting.
- **Excel percent formatting** — removed broken `abs(num) > 1` heuristic that treated values ≤1 as decimals (0.49% → 49%). Now always divides by 100 before Excel's `%` format.
- **Export title leak** — `suiteql-tool-card.tsx` was using raw `userQuestion` chat message as Excel title and filename. Changed to date-based `query-results-YYYY-MM-DD`.

## Resolved (2026-03-26)

- **BigQuery experiments fixed** — wrong credentials key (`service_account` → `service_account_json`), missing `location` param, Haiku preamble in SQL output. Added `_extract_sql()` to parse SQL from markdown/preamble, `_BIGQUERY_SCHEMA_HINT` with actual column names. BigQuery went from 0/15 to 11/15 KEEP.
- **SuiteQL schema hint** — added `_SUITEQL_SCHEMA_HINT` with common NetSuite tables (transaction, transactionline, customer, item, account, transactionaccountingline), key columns, JOIN patterns, and dialect quirks. SuiteQL went from 1/15 to 7/15 KEEP.
- **Confidence scoring fix** — financial reports now get floor of 4.0 (was ~2.4). Added `deterministic_success` flag to `CompositeScorer`. Added `netsuite_financial_report` and `bigquery_sql` to `data_tools` set.
- **Organic eval case mining** — new `eval_cases` table (migration 055), `eval_case_miner.py` mines chat_messages for confidence >= 4 queries, extracts keywords via Haiku ($0.03/case), deduplicates at 80% word overlap. Nightly task: mine → load seed+organic → run experiments. Organic cases prioritized.
- **Nightly improvement results** — 18/30 KEEP (60%) up from 3/30 (10%) at start of session. $5.25/run, 68 seconds. Runs at 5 AM UTC.
