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

## Architecture Invariants

- **Multi-tenant**: All tables have `tenant_id`. RLS via `SET LOCAL app.current_tenant_id` (use `set_tenant_context()`).
- **NetSuite Auth**: OAuth 2.0 PKCE; per-connection `client_id` (REST + MCP integration records). Never global.
- **Connection Setup**: REST API needs Account ID, Client ID, RESTlet URL (`metadata_json.restlet_url`). MCP needs Account ID, Client ID (separate Integration Record). Collected in `step-connection.tsx`, editable in `netsuite-connections-section.tsx`.
- **File Cabinet I/O**: `ecom_file_cabinet_restlet.js` does in-place load → set `.contents` → `.save()`.
- **SuiteQL paths**: Local REST (`netsuite_suiteql`) supports `customrecord_*`; external MCP (`ns_runCustomSuiteQL`) only standard tables.
- **Chat**: One unified agent. Domain context via YAML knowledge profiles (`backend/app/services/chat/knowledge_profiles/`). Model self-routes.
- **MCP write safety (HITL)**: `mutation_guard.classify_mutation()` intercepts; user approves via `WriteConfirmationCard`; HMAC token; system record types blocked.
- **Fiscal Calendar**: `tenant_configs.fiscal_year_start_month` injected as `## FISCAL CALENDAR` prompt block.
- **White-Label Branding**: Per-tenant brand_name/color/logo in `tenant_configs`; `BrandingProvider` injects `--primary`.
- **Feature Flags**: `tenant_feature_flags` table; `require_feature(flag_key)` returns 403 when disabled.
- **Soul config**: file-based at `/tmp/workspace_storage/{tenant_id}/soul.md`. NEVER overwrite/seed without explicit user confirmation.
- **react-resizable-panels v4**: Imports: `Panel`, `Group as PanelGroup`, `Separator as PanelResizeHandle`. Uses `orientation` prop (not `direction`). (Will move to frontend rules file.)

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
| Knowledge profile loader | `backend/app/services/chat/knowledge_profiles/loader.py` |
| Prompt assembler | `backend/app/services/chat/prompt_assembler.py` |
| Tool inventory + category registry | `backend/app/services/chat/tool_inventory.py`, `tool_categories.py` |
| Mutation guard + write confirmation | `backend/app/services/chat/mutation_guard.py`, `write_confirmation_service.py` |
| Capability-sync CI invariant | `backend/tests/test_prompt_tool_sync.py` |
| Permission helpers | `backend/app/core/dependencies.py` |
| API client (frontend) | `frontend/src/lib/api-client.ts` |
| SSE chat stream normalizer | `frontend/src/lib/chat-stream.ts` |
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
23. **MCP CRUD requires guardrails** — `ns_createRecord`/`ns_updateRecord` MUST NOT auto-execute. Always: (1) show payload, (2) get HITL confirmation, (3) for updates show before/after via `ns_getRecord`, (4) audit log. System record types (employee, role, account, subsidiary, etc.) are blocked entirely via `_BLOCKED_RECORD_TYPES` in `mutation_guard.py`.
24. **Unified agent prompt MUST stay in sync with SuiteQL agent** — both contain SuiteQL dialect rules. Copy verbatim — never paraphrase. Each rule prevents a specific production failure.
25. **External MCP response format differs** — `ns_runCustomSuiteQL` returns `{"data": [{col: val}], "queryExecuted": "...", "resultCount": N}`, NOT `{"columns": [], "rows": []}`. Test interception with both formats.
26. **Use `print(flush=True)` for docker logging** — structlog doesn't surface stdlib `logger.info` in docker logs.
27. **Knowledge profiles use fnmatch globs** — `ext__*__ns_createRecord` matches any MCP connector UUID. Use `*` for variable segments, never hardcode connector UUIDs in profile trigger_tools.
28. **One unified agent, no routing** — all queries go to UnifiedAgent. Domain context injected via knowledge profiles based on which tools are present. No three-tier routing, no specialist agents, no agent registry.
29. **Profile prompt fragments must stay in sync** — if you update SuiteQL rules in the unified agent prompt, also update the relevant knowledge profile YAML (and vice versa). Same verbatim-copy rule as before.
30. **RAG partitions are per-profile** — `collect_rag_partitions()` gathers partition IDs from active profiles. The `partition_ids` parameter on `retrieve_domain_knowledge()` filters chunks. Don't query all partitions when only specific profiles are active.
31. **Benchmark against native Claude + MCP** — every change to chat/agent/profile code must pass the vs-MCP benchmark (16+ wins out of 18). CI gate enforces on PRs.
32. **BigQuery tool names use dots in registry but underscores in LLM** — `bigquery.sql` in tool registry becomes `bigquery_sql` for the LLM. The name sanitizer handles this automatically.
33. **BigQuery uses LIMIT not FETCH FIRST** — `FETCH FIRST N ROWS ONLY` is SuiteQL syntax. BigQuery Standard SQL uses `LIMIT N`.
34. **Chart extraction happens post-stream** — same pattern as confidence tag extraction. `extract_charts()` runs after full response, emits `chart` SSE events.
35. **Soul config is file-based** — stored at `/tmp/workspace_storage/{tenant_id}/soul.md`. Must have persistent Docker volume. NEVER overwrite or seed without explicit user confirmation.
36. **Financial reports use _FINANCIAL_RE regex** — detects financial report intent for task augmentation. No routing veto needed since there's no specialist routing anymore.
37. **nginx ssl_buffer_size for SSE** — default 16KB causes bursty streaming over TLS. Set to 4k for real-time SSE.
43. **`normalizeStreamMessage` must preserve `structured_output`** — when adding new structured types, the SSE terminal `message` event's `structured_output` field MUST be copied in `frontend/src/lib/chat-stream.ts::normalizeStreamMessage()`. Otherwise the frontend drops it.
44. **`session.source_pin` is a prompt hint** — pin is injected via `build_source_pin_hint()` as a lightweight preference in the system prompt. The model decides whether to follow it based on the query. No routing override logic needed.
46. **One Next.js dev server per project, from the main checkout** — if you have worktrees, make sure you're not running `npm run dev` from a stale worktree. Check with `ps aux | grep next-dev` if hot reload isn't working.
38. **Stripe SDK v15 breaking changes** — `dict(payout)` fails (use `payout.to_dict()`). `account.get("field")` fails (use `getattr(account, "field", None)`). StripeObject no longer behaves like a dict.
39. **Stripe connector key in `connections` table** — encrypted per-tenant, NOT in env vars. `STRIPE_API_KEY` in config.py is for billing only. Per-connection key via `decrypt_credentials(connection.encrypted_credentials)["api_key"]`.
40. **Recon pipeline Stripe sync timeout** — initial sync pulls all historical payouts (800+) with payout lines — can take 30+ min. Pipeline has 90s timeout with fallback to existing data. Pre-sync via Settings "Sync Now" or nightly Beat schedule.
41. **Never let LLM present tool-computed numbers** — LLMs hallucinate/round numbers. Use tool result interception (`_intercept_tool_result`) to send data directly to frontend via SSE events (`data_table`, `task_output`). Condensed result to LLM should say "table shown automatically, do NOT list numbers." Pricing agent, SuiteQL, BigQuery all follow this pattern.
42. **Supabase 2min statement timeout** — batch commits every 10 rows for upserts. Stripe/NetSuite sync both hit this. Cursor must save `max(created)` not `last` (Stripe returns newest first).
47. **Initialize orchestrator variables before branch points** — variables used after if/elif chains in `run_chat_turn()` MUST be initialized before the chain. Chitchat path, picker-skip path, and other branches skip assignment blocks. `test_orchestrator_paths.py` catches this statically.
48. **`_validate_read_only` must strip SQL comments** — LLMs generate `-- comment\nSELECT...`. The `_strip_sql_comments()` helper removes `--` and `/* */` before the `startswith` check. Do NOT use `_strip_sql_comments` to transform queries before execution (doesn't handle string literals).
49. **`SessionDetailResponse` must include run fields** — `active_run_id`, `status`, `run_started_at` must be in BOTH `SessionListItem` and `SessionDetailResponse`. Missing them from detail broke SSE reconnection.
50. **Never hardcode tool names in agent prompts** (PR #37) — use the `{{TOOL_INVENTORY}}` placeholder, resolved at runtime by `_assemble_system_prompt` in `orchestrator.py` via `tool_inventory.build_tool_inventory_block`. The CI invariant `tests/test_prompt_tool_sync.py` fails if anyone reintroduces a tool name in a prompt that isn't in the schema. To add a tool's category, edit ONE place: `tool_categories.py::_EXACT`.
51. **LLM adapter SDK defaults will hang for 10 min** (PR #36) — `anthropic.AsyncAnthropic(api_key=...)` defaults to `read=600s`. Always pass `timeout=httpx.Timeout(connect=5, read=60, write=60, pool=60)` and `max_retries=2`. Same for `openai.AsyncOpenAI` and `genai.Client(http_options=...)`. Resolver-style optional pre-flight calls should also wrap in `asyncio.wait_for(timeout=15)` for graceful degradation.
52. **No more semantic router** (PR #40) — Tier 2 semantic routing deleted. The unified agent handles all queries. No need for conversation history in routing decisions.
53. **Auto source_pin from tool use** (PR #37) — when a turn calls `bigquery_*` (or `netsuite_*`), `_compute_source_pin_update(tool_calls_log)` updates `session.source_pin` post-turn so the next ambiguous query inherits the source. Mixed turns clear the pin.
54. **Capability-sync follow-ups** — (a) ✅ RESOLVED in PR #39. (b) ✅ RESOLVED: `_assemble_system_prompt` now appends tool inventory to templates missing `{{TOOL_INVENTORY}}` placeholder.
55. **Mutation tools require HITL confirmation flow** (PR #39) — `classify_mutation()` in `mutation_guard.py` detects write tools. The intercept in `base_agent.py::run_streaming()` yields `confirmation_required` BEFORE execution. Non-streaming `run()` blocks mutations entirely. Never bypass by calling `execute_tool_call()` directly for mutation tools without HMAC token validation via `validate_and_extract_confirmation()`.
56. **HMAC session binding for write tokens** (PR #39) — `generate_confirmation_token()` binds payload to session_id. When session_id is None, falls back to tenant_id (not empty string). Tokens are one-use: status changes from "pending" to "approved" only on successful execution. Failed executions keep status "pending" for retry.
57. **`write_confirm` follows the `source_pick` pattern** (PR #39) — reuses last user message (no duplicate), short-circuits before history/RAG assembly for efficiency. Same pattern: `SendMessageRequest` gets a new field, `chat.py` reuses last user msg, `orchestrator.py` handles at top of `run_chat_turn`.
58. **Anthropic adapter must allowlist tool fields** (PR #41) — `tools.py` stamps internal-only `category` onto every tool dict (lines 74-77, 195-198) for the prompt's tool-inventory block. The Anthropic adapter is identity-mapping, so any extra key reached the API and chat broke with `400 tools.0.custom.category: Extra inputs are not permitted` after PR #37. Fix: `_to_api_tool()` in `anthropic_adapter.py` allowlists `name`, `description`, `input_schema`, `cache_control`, `type`. Adding new internal-only tool metadata is safe — unknown keys are silently dropped. OpenAI/Gemini unaffected (their `_convert_tools` builds fresh dicts). Regression test: `tests/test_llm_adapters.py::TestAnthropicToolFieldStripping`.
59. **Deploy reports SUCCESS on partial image-pull failure** — `docker compose pull` runs backend AND frontend; if frontend `latest` 403s, the pull is interrupted but the post-deploy `curl /health` succeeds (old container still running, healthy) and the workflow reports green. Bit us with PR #41: backend image was 22min stale, fix appeared deployed but wasn't. To verify a deploy actually landed: `ssh aidenyi@34.73.236.64 "sudo docker inspect ecom-netsuite-backend-1 --format '{{.Image}}'"` and compare to the pushed image digest, not just container health. Action item: deploy workflow needs to fail-fast on pull errors and assert image digest match before reporting success.

## Known Issues

1. **LLM pivot limitation** — always use `pivot_query_result` tool, not CASE WHEN SQL.
2. **Proven patterns** — auto-learning from live sessions DISABLED (2026-04-09). Only admin-seeded or nightly-promoted patterns are retrievable. 6 verified shipping-country patterns + 1 RAG chunk seeded for Framework.
3. **Stripe initial sync is slow** — 400K+ payout lines, takes 30+ min first time. Batch commits every 200 lines. Hourly incremental via Beat after that.
4. **Agent benchmark vs MCP baseline** — our north star. Every change to chat/agent code must match or beat Claude+MCP. CI gate enforces this on PRs. Nightly cron tracks trends. See `memory/feedback_benchmark_vs_claude_mcp.md`.

## Skills Reference

Domain knowledge lives in `.claude/skills/`. Use the Skill tool to load when needed:

| Skill | Use For |
|-------|---------|
| `netsuite-mcp-chat` | Chat orchestration, knowledge profiles, tool interception, SSE streaming, entity resolution |
| `ai-agent-design` | Knowledge-driven agent architecture, YAML profiles, prompt assembly, benchmarks |
| `netsuite-mastery` | SuiteQL dialect, SuiteScript 2.x, REST API, OAuth, NetSuite tribal knowledge |
| `netsuite-reconciliation` | Reconciliation engine, order-level matching, data pipeline, Stripe sync, evidence packs |
| `bigquery-bi` | BigQuery BI agent, schema seeder, chart pipeline, connector lifecycle |
| `pricing-agent` | Currency conversion, PricingEngine, TemplateFiller, TaskFileService |
| `saas-deployment` | Docker, GCP, nginx, CI/CD, Alembic migrations, staging deploy procedures |
| `suitescript-engineer` | SuiteScript development, workspace, SDF, deploy pipeline |
| `autonomous-improvement` | Nightly eval/experiment loop, scoring, pattern promotion |
| `shopify-ops` | Shopify sync pipeline, order ingestion |

