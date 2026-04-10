# Ecom NetSuite Suites — Project Intelligence

> Read by Claude Code at session start. Encodes patterns, conventions, and decisions.

## Development Workflow — FOLLOW ALWAYS

- **TDD strictly**: Write failing tests FIRST, then implement. No production code without a failing test. This applies to EVERY task, regardless of whether the plan document explicitly says "TDD" — assume TDD unless the task is purely non-code (docs, config, infra).
- **Max 15 iterations per task**: Use the loop protocol. If blocked after 3 self-heal attempts within a task, stop and report. An "iteration" = one test-implement-verify cycle, OR one review-fix cycle. Review loops (spec + quality) count toward the 15.
- **Multi-agent execution**:
  - *Sequential implementers* — fresh subagent per task via `subagent-driven-development`. Never dispatch multiple implementation subagents in parallel against the same working tree (file conflicts).
  - *Parallel reviewers* — spec-reviewer + code-quality-reviewer run after each task (sequential within a task but fast).
  - *Parallel research* — use Explore / general-purpose agents in parallel when investigating independent questions.
  - *True parallel implementation* — only when tasks touch disjoint file sets (e.g., backend-only vs frontend-only) AND file ownership is pre-declared in the plan AND each runs in its own worktree.
- **Subagent dispatch checklist** — every implementer prompt I send must include:
  1. TDD required (write failing test first)
  2. Max 15 iterations per task; stop and report if blocked after 3 self-heal attempts
  3. Research existing code before writing
  4. Commit when green; one commit per logical change
  5. Files owned by this task (to prevent conflicts with other subagents)
  Subagents do NOT inherit `CLAUDE.md` — these rules must be in the dispatch prompt every time.
- **Zero regressions**: Run full test suite before committing. Fix CI as a follow-up after every deploy.
- **Discuss before fixing**: Always discuss approach AND research existing code before making changes.
- **Commit frequently**: One commit per logical change. Never amend. Push to BOTH repos (`origin` + `framework`).

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
- **Reconciliation Engine**: Three-tier deterministic matching (exact payout ID → fuzzy amount/date/narration → AI investigation for 5% exceptions). No LLM in matching pipeline. Chat agent as primary interface, dashboard as secondary.
- **Source Picker (v0.1)**: Confidence-gated pre-execution card picker between NetSuite and BigQuery. `score_source()` in `backend/app/services/chat/source_picker.py` returns `(source, confidence, reason)`. Threshold 0.85 — above = auto-run, below = picker. Financial keywords → NetSuite 0.99, marketing → BigQuery 0.95, NS entities → 0.95, ambiguous (orders/customers/revenue) → 0.55. Orchestrator short-circuits BEFORE agent execution, persists picker placeholder `ChatMessage` with `structured_output.type == "source_picker"`, yields terminal message, returns. User click posts `source_pick` field, backend sets `session.source_pin`, marks picker as `selected`, runs agent honoring pin. Routing block explicitly checks `source_pin` before 3-tier routing.
- **Fiscal Calendar**: `tenant_configs.fiscal_year_start_month` (1-12, default 1) injected into unified agent prompt as `## FISCAL CALENDAR` block. Agent interprets Q1/Q2/Q3/Q4/"fiscal year" using tenant's fiscal calendar instead of defaulting to calendar year. Calendar-year tenants (Framework) get the default behavior.

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
| Pricing engine | `backend/app/services/pricing_engine.py` |
| Pricing schemas | `backend/app/schemas/pricing.py` |
| Pricing config API | `backend/app/api/v1/pricing_config.py` |
| Pricing config service | `backend/app/services/pricing_config_service.py` |
| Pricing config defaults | `backend/app/services/pricing_config_defaults.py` |
| Pricing tools | `backend/app/mcp/tools/pricing_tools.py` |
| Template filler | `backend/app/services/template_filler.py` |
| Task file service | `backend/app/services/task_file_service.py` |
| Task files API | `backend/app/api/v1/task_files.py` |
| Agent instructions API | `backend/app/api/v1/agent_instructions.py` |
| Task output card | `frontend/src/components/chat/task-output-card.tsx` |
| File upload zone | `frontend/src/components/chat/file-upload-zone.tsx` |
| Instruction panel | `frontend/src/components/chat/instruction-panel.tsx` |
| Template slot | `frontend/src/components/chat/template-slot.tsx` |
| Specs / Plans | `docs/superpowers/specs/`, `docs/superpowers/plans/` |
| Architecture memory | `memory/` |
| Reconciliation engine | `backend/app/services/reconciliation/` |
| Recon API | `backend/app/api/v1/reconciliation.py` |
| Recon agent config | `backend/app/services/chat/agents/configs/recon_agent.yaml` |
| Recon dashboard | `frontend/src/app/(dashboard)/reconciliation/` |
| Financial veto | `backend/app/services/chat/orchestrator.py` (_FINANCIAL_VETO_PHRASES) |
| Source picker scorer | `backend/app/services/chat/source_picker.py` |
| Source picker card | `frontend/src/components/chat/source-picker-card.tsx` |
| Connector status API | `backend/app/api/v1/connector_status.py` |
| Stripe sync service | `backend/app/services/ingestion/stripe_sync.py` |
| NetSuite deposit sync | `backend/app/services/ingestion/netsuite_deposit_sync.py` |
| Recon pipeline | `backend/app/services/reconciliation/pipeline.py` |
| Stripe health check | `backend/app/workers/tasks/stripe_health_check.py` |
| Stripe sync all (Beat) | `backend/app/workers/tasks/stripe_sync_all.py` |
| Recon progress stepper | `frontend/src/components/reconciliation/recon-progress-stepper.tsx` |
| Data freshness banner | `frontend/src/components/reconciliation/data-freshness-banner.tsx` |
| Stripe connector card | `frontend/src/components/settings/stripe-connector-card.tsx` |
| Data source connectors | `frontend/src/components/settings/data-source-connectors-section.tsx` |
| Permission helpers | `backend/app/core/dependencies.py` (require_any_permission) |
| Streaming tool card | `frontend/src/components/chat/streaming-tool-card.tsx` |
| Chat run manager | `backend/app/services/chat/run_manager.py` |
| Chat runs API | `backend/app/api/v1/chat_runs.py` |
| Frontend tests | `frontend/src/components/chat/__tests__/` |
| Vitest config | `frontend/vitest.config.ts` |
| nginx config | `/etc/nginx/sites-available/suitestudio` (on GCP VM) |
| Benchmark CLI | `backend/tests/agent_benchmarks/run_vs_mcp.py` |
| Benchmark cases | `backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/` |
| Baseline runner | `backend/tests/agent_benchmarks/baseline_runner.py` |
| Agent runner | `backend/tests/agent_benchmarks/agent_runner.py` |
| Benchmark scorer | `backend/tests/agent_benchmarks/scorer.py` |
| Benchmark persistence | `backend/tests/agent_benchmarks/persistence.py` |
| Benchmark API | `backend/app/api/v1/agent_benchmarks.py` |
| Benchmark nightly task | `backend/app/workers/tasks/agent_benchmark_vs_mcp.py` |
| Benchmark email | `backend/app/services/benchmark_email_service.py` |
| History tool trace | `backend/app/services/chat/history_tool_trace.py` |
| CI benchmark gate | `.github/workflows/agent-benchmark.yml` |

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
35. **Soul config is file-based** — stored at `/tmp/workspace_storage/{tenant_id}/soul.md`. Must have persistent Docker volume. NEVER overwrite or seed without explicit user confirmation.
36. **Financial routing needs veto, not just regex** — `_FINANCIAL_VETO_PHRASES` catches plurals/variants that the coordinator regex misses. Applied after Tier 1, session pin, and Tier 2 in `_select_agent()`.
37. **nginx ssl_buffer_size for SSE** — default 16KB causes bursty streaming over TLS. Set to 4k for real-time SSE.
43. **`normalizeStreamMessage` must preserve `structured_output`** — when adding new structured types (like source_picker), the SSE terminal `message` event's `structured_output` field MUST be copied in `frontend/src/lib/chat-stream.ts::normalizeStreamMessage()`. Otherwise the frontend drops it. This bit us twice.
44. **`session.source_pin` must be honored by routing** — setting `source_pin` alone doesn't change behavior. The orchestrator's routing block explicitly checks `session.source_pin` BEFORE calling `_select_agent()`. Bypass Tier 1/2 when pin is set.
45. **Source picker placeholders do NOT re-persist user messages** — when `source_pick` is present in the POST body, reuse the last existing user message via `SELECT ... ORDER BY created_at DESC LIMIT 1`. Creating a new user row duplicates the question in the conversation.
46. **One Next.js dev server per project, from the main checkout** — if you have worktrees, make sure you're not running `npm run dev` from a stale worktree. Check with `ps aux | grep next-dev` if hot reload isn't working.
38. **Stripe SDK v15 breaking changes** — `dict(payout)` fails (use `payout.to_dict()`). `account.get("field")` fails (use `getattr(account, "field", None)`). StripeObject no longer behaves like a dict.
39. **Stripe connector key in `connections` table** — encrypted per-tenant, NOT in env vars. `STRIPE_API_KEY` in config.py is for billing only. Per-connection key via `decrypt_credentials(connection.encrypted_credentials)["api_key"]`.
40. **Recon pipeline Stripe sync timeout** — initial sync pulls all historical payouts (800+) with payout lines — can take 30+ min. Pipeline has 90s timeout with fallback to existing data. Pre-sync via Settings "Sync Now" or nightly Beat schedule.
41. **Never let LLM present tool-computed numbers** — LLMs hallucinate/round numbers. Use tool result interception (`_intercept_tool_result`) to send data directly to frontend via SSE events (`data_table`, `task_output`). Condensed result to LLM should say "table shown automatically, do NOT list numbers." Pricing agent, SuiteQL, BigQuery all follow this pattern.
42. **Supabase 2min statement timeout** — batch commits every 10 rows for upserts. Stripe/NetSuite sync both hit this. Cursor must save `max(created)` not `last` (Stripe returns newest first).

## Current State

- **Product**: AI-den v1.7 deployed to staging 2026-04-10. Agent quality overhaul (PR #33), vs-MCP benchmark harness, pattern cleanup, retrieval thresholds.
- **Latest migration**: 066_bench_vs_mcp (agent_benchmark_runs table)
- **Frontend tests**: Vitest + @testing-library/react (30 tests). Run: `cd frontend && npx vitest run`
- **Backend tests**: 140+ tests. Run: `cd backend && .venv/bin/python -m pytest`
- **Agent benchmark**: 18 sales cases vs Claude+MCP. Run: `cd backend && .venv/bin/python -m tests.agent_benchmarks.run_vs_mcp --suite sales --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a`
- **Staging**: `api-staging.suitestudio.ai` + `staging.suitestudio.ai`. GCP Docker + nginx + Let's Encrypt. Deploy: `saas-deployment` skill.
- **Nightly benchmark**: 11:00 UTC, enabled on staging. Results in `agent_benchmark_runs` table. Regression alerts via Sentry + structured log.
- **Nightly auto-improvement**: 10:00 UTC. KEEP/REVERT/SKIP decisions now based on vs-MCP comparison (not broken composite scorer).
- **Auto-learning from live chat**: DISABLED (pattern pollution source). Patterns only via admin seed or eval-gated nightly promotion.
- **Pattern retrieval threshold**: ≥ 0.45 similarity. Domain knowledge threshold: ≥ 0.50. Learned rules: query-aware (max 10 relevant).
- **MCP tool descriptions**: NO local caps. Oracle's full descriptions flow through to the agent.

## Known Issues

1. **LLM pivot limitation** — always use `pivot_query_result` tool, not CASE WHEN SQL.
2. **Proven patterns** — auto-learning from live sessions DISABLED (2026-04-09). Only admin-seeded or nightly-promoted patterns are retrievable. 6 verified shipping-country patterns + 1 RAG chunk seeded for Framework.
3. **Stripe initial sync is slow** — 400K+ payout lines, takes 30+ min first time. Batch commits every 200 lines. Hourly incremental via Beat after that.
4. **Confidence scorer partially broken** — `query_pattern_similarity` zeroed out (was part of feedback loop). LLM self-score and tool_success_rate are the remaining signals. `final_text[:500]` truncation in confidence extractor still open.
5. **Agent benchmark vs MCP baseline** — our north star. Every change to chat/agent code must match or beat Claude+MCP. CI gate enforces this on PRs. Nightly cron tracks trends. See `memory/feedback_benchmark_vs_claude_mcp.md`.

## Skills Reference

Domain knowledge lives in `.claude/skills/`. Use the Skill tool to load when needed:

| Skill | Use For |
|-------|---------|
| `netsuite-mcp-chat` | Chat orchestration, agent routing, tool interception, SSE streaming, entity resolution |
| `ai-agent-design` | Agent framework v1.1, composition + hooks, three-tier routing, YAML configs, benchmarks |
| `netsuite-mastery` | SuiteQL dialect, SuiteScript 2.x, REST API, OAuth, NetSuite tribal knowledge |
| `netsuite-reconciliation` | Reconciliation engine, order-level matching, data pipeline, Stripe sync, evidence packs |
| `bigquery-bi` | BigQuery BI agent, schema seeder, chart pipeline, connector lifecycle |
| `pricing-agent` | Currency conversion, PricingEngine, TemplateFiller, TaskFileService |
| `saas-deployment` | Docker, GCP, nginx, CI/CD, Alembic migrations, staging deploy procedures |
| `suitescript-engineer` | SuiteScript development, workspace, SDF, deploy pipeline |
| `autonomous-improvement` | Nightly eval/experiment loop, scoring, pattern promotion |
| `shopify-ops` | Shopify sync pipeline, order ingestion |

## Resolved History

Full changelog moved to skills. Key milestones:
- **v1.0** (2026-03-18): Token refresh, entity seeder, 10x agent quality
- **v1.1** (2026-03-23): Agent framework, BigQuery BI, chart pipeline, scalability
- **v1.2** (2026-03-27): Pricing Agent, Agent Hub, follow-up intelligence, autonomous improvement
- **v1.3** (2026-03-29): Reconciliation engine, data pipeline connectors, GCP frontend
- **v1.5** (2026-03-30): Self-service sync, order-level matching, progress stepper, CI green
- **v1.6** (2026-04-03): Background chat, streaming tool cards, ordered content blocks, trimmed prompt
- **v0.1 Intent Clarification** (2026-04-09): Source picker cards (confidence-gated, ambiguous → two cards, < 0.85 threshold), fiscal calendar injection into agent prompts, abandoned v0 disclosure footer design after design mismatch
