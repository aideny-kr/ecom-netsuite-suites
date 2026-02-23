# Gemini Agent Context — Hard Rules

> Give this file to Gemini at the start of every session.
> These rules are non-negotiable and override any assumptions.

## SuiteQL Dialect (Oracle-based)

**ROWNUM vs FETCH FIRST — CRITICAL:**
- `ROWNUM` is evaluated BEFORE `ORDER BY`. This means `WHERE ROWNUM <= 1 ORDER BY date DESC` returns a RANDOM row, NOT the latest.
- For "latest N" / "top N" queries: ALWAYS use `ORDER BY ... FETCH FIRST N ROWS ONLY`.
- `ROWNUM` is ONLY safe for unordered limiting (e.g., `WHERE ROWNUM <= 100` without ORDER BY).
- `LIMIT` is NOT supported in SuiteQL. Never use it.
- The governance layer appends `FETCH FIRST 50 ROWS ONLY` as a safety net for unbounded queries in `mcp_client_service.py`. Do not duplicate this in the agent prompt.

**Two SuiteQL execution paths — CRITICAL:**
- **Local REST API** (`netsuite_suiteql` tool): POST `/services/rest/query/v1/suiteql`. Supports ALL tables including `customrecord_*` and `customlist_*`. The allowlist dynamically permits these prefixes.
- **External MCP** (`ns_runCustomSuiteQL`): NetSuite MCP endpoint `/services/mcp/v1/all`. Works ONLY for standard tables (transaction, customer, item, etc.). Returns "Record not found" for custom record tables.
- Agent prompt instructs: use local tool for custom records, external MCP for standard tables.

## Port Numbers

| Service | Port | Notes |
|---------|------|-------|
| Backend API | 8000 | FastAPI via Docker |
| Frontend (Docker) | 3002 | Docker maps container:3000 → host:3002 |
| Frontend (local dev) | 3000 | `npm run dev` |
| PostgreSQL | 5432 | |
| Redis | 6379 | |

- **Playwright E2E tests** use `baseURL: "http://localhost:3002"` (Docker) by default.
- **CORS allowed origins**: `http://localhost:3000`, `http://localhost:3002`.
- Do NOT change the Playwright baseURL or add a `webServer` config without confirming with the team.

## Frontend Rules

- **Never use raw `fetch()`** — always use `apiClient` from `@/lib/api-client.ts`.
- If you need SSE streaming, use `apiClient.stream()` which handles auth + base URL.
- Auth tokens are stored in `localStorage` and injected by `apiClient`. Do not manually read `localStorage` in components.
- Icons: `lucide-react` only.
- We replaced Monaco Editor with a plain text `<CodeViewer>` component. Do not reference `.monaco-editor` selectors.

## Backend Rules

- **LLM Adapter pattern**: `BaseLLMAdapter` has `create_message()` (abstract) and `stream_message()` (default fallback). If you add a new method to the base class, it MUST have a default implementation OR you must implement it in ALL adapters (Anthropic, OpenAI, Gemini).
- `run_chat_turn()` is an **async generator** that yields SSE event dicts, NOT an async function returning `ChatMessage`. The final message is yielded as `{"type": "message", "message": {...}}`.
- The chat endpoint returns `StreamingResponse(media_type="text/event-stream")`, not JSON.
- Always use `Annotated[Type, Depends()]`, never bare `Depends()`.
- Always use `mapped_column()`, never `Column()`.
- Always `await db.commit()` after mutations.

## Docker

- The frontend uses an **anonymous volume** for `node_modules`. When adding npm packages, you MUST rebuild with `--renew-anon-volumes`:
  ```
  docker compose up -d --build --renew-anon-volumes frontend
  ```

## Testing

- Backend tests: `backend/.venv/bin/python -m pytest backend/tests/`
- Frontend build check: `cd frontend && npm run build`
- The test `test_mcp_success_skips_rest` is a known pre-existing failure — ignore it.
- When mocking `run_chat_turn`, remember it's an async generator. Use `async for` or wrap in a helper.
- When mocking the LLM adapter, you must mock BOTH `create_message` (for multi-agent specialist calls) AND `stream_message` (for single-agent orchestrator loop).

## Architecture Decisions Already Made

- **Semantic Routing Engine**: `coordinator.py` uses a fast regex/keyword heuristic classifier (`classify_intent()`) that skips the LLM planning call for clear-cut queries. Only ambiguous queries fall back to Haiku LLM planning. Do not revert to LLM-only planning.
- **Governance Intercept**: `mcp_client_service.py` appends `FETCH FIRST 50 ROWS ONLY` to unbounded SuiteQL queries and enforces a 15-second timeout. This is the safety net — do not remove it. Do NOT use subquery wrapping (`SELECT * FROM (subquery) WHERE ROWNUM <= 50`) as it breaks field access (e.g., `mainline`).
- **Multi-agent coordinator**: Routes are defined in `ROUTE_REGISTRY` dict. To add a new agent, add it to the registry and `_create_agent()` factory. Do not restructure the routing architecture.
- **Tenant-aware entity resolution**: Runs before SuiteQL agent dispatch in `coordinator.py`. Pipeline: Haiku NER extraction → pg_trgm fuzzy matching (`tenant_entity_mapping` table with composite GIN index) → `<tenant_vernacular>` XML block injected into agent's system prompt via `context["tenant_vernacular"]`. Entity mappings are seeded from metadata discovery (`tenant_entity_seeder.py`, called in `netsuite_metadata_service.py`). Do not bypass or restructure this pipeline.
- **Token refresh**: Uses `commit()` (not `flush()`) to persist OAuth refresh tokens immediately. Single-use refresh tokens from NetSuite are invalidated on use — if the new token isn't committed, the connection is permanently broken.

## Chat Agent Architecture

- **Coordinator** (`coordinator.py`): Supervisor with semantic routing. Heuristic classifier → route registry → specialist agents.
- **Specialist agents** in `agents/`: SuiteQLAgent (6 steps), RAGAgent (2 steps), DataAnalysisAgent, WorkspaceAgent. Each runs an agentic loop with tool calls.
- **Entity resolver** (`tenant_resolver.py`): Extracts entities via Haiku, matches against `tenant_entity_mapping` using `similarity()` + `%` operator, produces XML.
- **LLM adapters** (`adapters/`): Anthropic, OpenAI, Gemini. All implement `create_message()` (sync) and `stream_message()` (async generator).
- **SuiteQL agent prompt**: Has `<tenant_vernacular>` injection, mandatory custom record matching (Step 0), no-guessing rule, tool selection guidance.
- **Latest migration**: 025_tenant_entity_mapping (pg_trgm + btree_gin extensions, composite GIN index)

## Key File Paths

| What | Where |
|------|-------|
| Chat coordinator | `backend/app/services/chat/coordinator.py` |
| Chat orchestrator | `backend/app/services/chat/orchestrator.py` |
| SuiteQL agent | `backend/app/services/chat/agents/suiteql_agent.py` |
| RAG agent | `backend/app/services/chat/agents/rag_agent.py` |
| Entity resolver | `backend/app/services/chat/tenant_resolver.py` |
| Entity seeder | `backend/app/services/tenant_entity_seeder.py` |
| Entity mapping model | `backend/app/models/tenant_entity_mapping.py` |
| LLM adapters | `backend/app/services/chat/adapters/` |
| MCP governance | `backend/app/services/mcp_client_service.py` |
| SuiteQL local tool | `backend/app/mcp/tools/netsuite_suiteql.py` |
| Metadata discovery | `backend/app/services/netsuite_metadata_service.py` |

## Common Mistakes to Avoid

1. Do not use `ROWNUM` with `ORDER BY` for top-N queries.
2. Do not use raw `fetch()` in frontend components.
3. Do not add abstract methods to base classes without implementing in all subclasses.
4. Do not change port numbers in test configs without checking Docker mappings.
5. Do not reference `.monaco-editor` — it was replaced.
6. Do not change `run_chat_turn` signature without updating all 6 test files.
7. Do not use `flush()` for OAuth token persistence — use `commit()`.
8. Do not add `webServer` to Playwright config — tests run against Docker.
9. Do not wrap SuiteQL in subqueries for governance — use `FETCH FIRST N ROWS ONLY` (subquery breaks field access like `mainline`).
10. Do not guess custom record table names — use the `<tenant_vernacular>` resolved script IDs or `netsuite_get_metadata` to discover them.
11. Do not use external MCP (`ns_runCustomSuiteQL`) for `customrecord_*` tables — it returns "Record not found". Use the local `netsuite_suiteql` tool instead.
