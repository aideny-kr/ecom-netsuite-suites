# Spec — Phase 1: Cross-Source Join Engine (`cross_source_query` tool)

- **Date:** 2026-06-03
- **Branch:** `feat/cross-source-join-engine` (off `research/cross-source-analytics`)
- **Parent research:** `docs/superpowers/research/2026-06-03-cross-source-analytics-and-drive-rag.md` (Phase 1 of the 6-phase roadmap)
- **Status:** Draft for review → plan → TDD build
- **Decisions locked:** DuckDB engine (no feature flag); re-run queries for full rows (cached results are 50-row previews); defer row-normalizer unification to a follow-up PR.

---

## 1. Goal

Give the unified agent a **deterministic backend tool that joins two data sources** (NetSuite SuiteQL × BigQuery) into one unified table, computed in the backend and rendered through the existing `data_table` trust boundary — so the LLM **orchestrates and narrates but never does the join math**.

This replaces today's "join" — two prose strings (`cross_source.yaml` step 3 + `prompt_assembler.DISAMBIGUATION_INSTRUCTION`) telling the model to "correlate the results in your response" from two ≤30-row previews — with a real, correct join.

## 2. Non-Goals (explicitly deferred)

- **Drive as a join source** (Phase 3 — `sheets.read_range` → joinable table).
- **Governed metric catalog / semantic layer** (Phase 2).
- **Unifying the 3 duplicate row-normalizers** (`netsuite_suiteql.collect_columns`, `_intercept_tool_result` MCP handling, `tool_call_results._extract_items_as_table`) — hot-path regression risk; separate PR.
- **Persisting full result sets** (Postgres staging table) — Phase 1 re-runs queries instead; staging is the documented later upgrade if double-query latency hurts.
- **3-way+ joins, window functions, fuzzy/range join keys** — Phase 1 is 2-source equality joins.

## 3. The Tool Contract

New local tool `cross_source.query` (LLM-facing name `cross_source_query`).

**Input params:**
```jsonc
{
  "left_query":   "string, required — SuiteQL or BigQuery SQL for source A",
  "left_dialect": "enum: suiteql | bigquery, required",
  "right_query":  "string, required — SQL for source B",
  "right_dialect":"enum: suiteql | bigquery, required",
  "join_keys":    "array, required — [{left: 'colA', right: 'colB'}] (1+ equality pairs)",
  "join_type":    "enum: inner | left, default inner",
  "select":       "array, optional — output columns (default: all, dedup-suffixed on collision)",
  "aggregations": "array, optional — [{column, fn: sum|count|avg|min|max, as}]",
  "pivot":        "object, optional — {row_field, column_field, value_field, aggregation} (reuses pivot_service.pivot_rows verbatim)"
}
```
The LLM passes **both queries it would otherwise run separately** plus the join key — a single tool call runs both, joins, and returns. (It does NOT first run `bigquery_sql`/`netsuite_suiteql` separately; the prompt directs it straight to `cross_source_query` for cross-source asks.)

**Output (the standard envelope, so `_intercept_tool_result` `data_table` path renders it with zero new SSE work):**
```jsonc
{
  "columns": ["..."],
  "rows": [["..."], ...],
  "row_count": 123,
  "joined": true,
  "join_type": "inner",
  "left_truncated": false,   // true if source A hit its row cap
  "right_truncated": false,  // true if source B hit its row cap
  "left_row_count": 4200,
  "right_row_count": 980,
  "warnings": ["..."]        // e.g. truncation, no matched rows, type-coerced key
}
```

## 4. Architecture & Data Flow

```
LLM (cross-source ask)
  └─ calls cross_source_query(left_query, right_query, join_keys, ...)
       └─ cross_source_tool.execute(params, context)         # context: {db, tenant_id, conversation_id, actor_id}
            ├─ fetch LEFT:  _run_source(left_query, left_dialect, context)   # reuse pivot_tool's per-dialect path
            ├─ fetch RIGHT: _run_source(right_query, right_dialect, context) # both limit-stripped + bounded
            ├─ normalize each → {columns, rows, dtypes}       # new canonical normalize_rows() (engine ingestion layer)
            ├─ duckdb (:memory:, ephemeral) via asyncio.to_thread:
            │     register(left), register(right) → JOIN (+ optional agg) → fetch {columns, rows}
            ├─ optional pivot via pivot_service.pivot_rows()  # reused verbatim
            └─ return {columns, rows, row_count, truncation flags, warnings}
  └─ _intercept_tool_result (category=data_table)             # full rows → SSE data_table; condensed preview → LLM
  └─ LLM narrates over the preview (never re-lists numbers)
```

**Source fetch reuse.** `_run_source` mirrors `pivot_tool._execute_suiteql_pivot` / `_execute_bigquery_pivot` (`backend/app/mcp/tools/pivot_tool.py`): resolve connection/creds from `context["db"]` + `context["tenant_id"]`, strip the row limit (`_strip_row_limit`), re-run via `execute_suiteql_via_rest(..., limit=CAP)` or `bigquery_service.execute_query(..., max_rows=CAP)`. Both already return `{columns, rows, truncated}`.

**Engine.** Ephemeral in-memory DuckDB per call. Register each source's normalized rows as a relation; run the join in DuckDB SQL (handles cross-source key **type coercion**, null semantics, outer joins, aggregation). DuckDB only does the JOIN/agg; the optional crosstab reuses `pivot_service.pivot_rows()`.

## 5. Tenant Isolation & Trust Boundary

- **Isolation:** re-running through `netsuite_suiteql.execute` / `bigquery_service.execute_query` means each source fetch is already filtered by `context["tenant_id"]` (`Connection.tenant_id` / `McpConnector.tenant_id`). The DuckDB join only ever sees rows already scoped to the tenant — **no new RLS surface, no cross-tenant key risk** (unlike reading the conversation cache, whose Redis key has no tenant prefix). The chat background session has no `set_tenant_context`, so we rely on this explicit threading — which the existing source tools already do.
- **Trust boundary (CLAUDE.md #6):** the tool MUST be categorized `data_table` in `tool_categories._EXACT` so `_intercept_tool_result` emits the full table as a `data_table` SSE event and hands the LLM only a condensed "table shown automatically — do not reproduce numbers" preview. The joined numbers must never be returned raw for the LLM to recite.

## 6. DuckDB Runtime Safety

Chat tools run in the **FastAPI web process** (via `asyncio.create_task`, not Celery), so:
- **Offload** the blocking DuckDB work with `await asyncio.to_thread(...)` so SSE for other tenants isn't stalled.
- **Per-call ephemeral connection:** `duckdb.connect(":memory:")` opened at execute start, `close()` in `finally`. No singleton/module-level connection (not thread-safe).
- **Bounded on the e2-small (~2GB RAM):** `SET memory_limit='256MB'`, `SET threads=1`, `SET temp_directory='<writable tmp>/duckdb'`.
- **No network extensions / autoload** — join operates on in-memory Python rows only.

## 7. Wiring Checklist (from the integration audit — every file to touch)

**Required (tool is uncallable without all 5):**
1. **Tool module** — new `backend/app/mcp/tools/cross_source_tool.py`: `async def execute(params, context=None, **kwargs) -> dict`, reads `context["db"]`/`context["tenant_id"]` like `pivot_tool.py:155-161`.
2. **Registry** — `backend/app/mcp/registry.py`: import `cross_source_tool`; add `"cross_source.query"` entry to `TOOL_REGISTRY` (template: the `pivot.query_result` block ~`registry.py:86-133`). `MCPServer` binds `TOOL_REGISTRY` automatically.
3. **Allowlist** — `backend/app/services/chat/nodes.py:31` `ALLOWED_CHAT_TOOLS`: add `"cross_source.query"` (next to `"pivot.query_result"`). Drives both `build_local_tool_definitions` visibility and `_LOCAL_NAME_MAP` reverse-lookup in `execute_tool_call` (`tools.py:64,77,255`). Dot→underscore makes the LLM name `cross_source_query`.
4. **Category** — `backend/app/services/chat/tool_categories.py` `_EXACT`: add `"cross_source_query": "data_table"` **and** `"cross_source.query": "data_table"`. (Single-source-of-truth per chat-orchestration rule #20.)
5. **Prompt routing line** — `backend/app/services/chat/agents/unified_agent.py` `<tool_selection>`/`<agentic_workflow>` (~`:165`/`:212`): one line describing when to use `cross_source_query`. **CI-gated** by `backend/tests/test_prompt_tool_sync.py` (tool-in-prompt must be a registered tool, and vice-versa).

**Optional (do them — cross-source by nature):**
6. **Plan-mode** — `backend/app/services/chat/plan_mode/short_circuit.py:437` `_CROSS_SOURCE_TOOLS`: add `"cross_source_query"` so source-pinning doesn't filter it out.
7. **Profile** — `backend/app/services/chat/knowledge_profiles/cross_source.yaml`: add `cross_source_query` to `trigger_tools` and put usage rules in `prompt_fragment`.

## 8. Dependencies
- Add `duckdb` to `backend/pyproject.toml` (and refresh `uv.lock`).
- Ensure it installs in `backend/Dockerfile.prod` (DuckDB ships a self-contained linux/amd64 wheel, no system libs).
- `pyarrow` optional — can register Python lists directly; avoid adding pyarrow unless needed.

## 9. Prompt Changes (both surfaces, in lockstep — chat-orchestration rule #5)

**A. `cross_source.yaml:12`** — replace step 3:
> *before:* `3. Correlate the results in your response — present a unified answer, not two separate tables`
> *after:* `3. Call cross_source_query with both queries and the join key — it joins the two sources and returns one unified table (rendered automatically; do not re-list the numbers).`

**B. `prompt_assembler.py:22-23`** (`DISAMBIGUATION_INSTRUCTION`, fires when ≥2 profiles active):
> *before:* `…call both tools and synthesize the results. Identify the join key…`
> *after:* `…if the question needs both sources, call cross_source_query (passing both queries + the join key) to merge them. Do not synthesize/correlate the two tables by hand — the join tool produces the unified result.`

## 10. Error Handling & Edge Cases
- **Source query error** (bad SQL, connection missing): return a structured error naming which side failed; do not crash the turn.
- **Truncation skew:** if either side hits its row cap, set `left_truncated`/`right_truncated` and add a `warnings` entry — the join is partial; surface it rather than silently dropping rows. (Consider refusing on double-truncation.)
- **No matched rows:** return empty `rows` with a `warnings` note (likely a join-key mismatch) rather than an error.
- **Join-key type mismatch** (e.g. SuiteQL `'12345'` string vs BigQuery `12345` int): DuckDB coercion handles it; add a `warnings` note when coercion was applied.
- **Missing/invalid `join_keys` columns:** validate against fetched columns; structured error listing available columns.
- **Row caps:** explicit per-side cap (start ~10k each, matching the pivot path); make it a constant, surfaced in `warnings` when hit.

## 11. Testing Strategy (TDD — write failing tests first)
- **Pure engine unit tests** (no DB): `normalize_rows()` (REST items / MCP `data`+`items` / pre-shaped `{columns,rows}` / dtypes); the DuckDB join (inner, left, multi-key, type-coerced key, no-match, aggregation, optional pivot). Fast, deterministic.
- **Tool `execute` integration** (mock `_run_source`): correct envelope, truncation flags, error shapes, tenant_id threaded into both fetches.
- **Wiring/CI:** `test_prompt_tool_sync.py` passes; tool appears in `build_all_tool_definitions`; categorized `data_table`; `_intercept_tool_result` condenses it (no raw numbers to LLM).
- **Tenant isolation:** assert both source fetches receive `context["tenant_id"]`; no path reads cross-conversation/cross-tenant state.
- **Runtime:** DuckDB work runs via `to_thread`; connection closed in `finally`; `memory_limit`/`threads`/`temp_directory` set.
- **Benchmark gate:** the cross-source scenario passes the existing vs-Claude+MCP benchmark (CLAUDE.md invariant).

## 12. Open Questions (resolve during plan/build)
1. Per-side row cap value (10k each? configurable via `config.py`?) and behavior on double-truncation (warn vs refuse).
2. Does `cross_source_query` accept raw queries only, or also `message_id` refs whose `query_text` it re-runs? (Spec assumes raw queries; refs are a possible convenience add.)
3. Output column collision policy (suffix `_left`/`_right`? source-prefix?).
4. Should `aggregations`/`pivot` ship in Phase 1 or be a fast-follow (join-only first)?
5. Exact `temp_directory` path under the container's writable area.

## 13. Acceptance Criteria
- A cross-source question ("compare NetSuite sales vs BigQuery marketing spend by SKU") yields **one joined `data_table`**, computed in DuckDB, with correct numbers on >50-row datasets — not two separate grids or LLM-eyeballed prose.
- The LLM never emits the joined numbers (interception holds); it narrates over the rendered table.
- Tenant isolation verified; `test_prompt_tool_sync.py` and the full suite green; benchmark ≥ baseline.
- DuckDB runs offloaded, bounded, and ephemeral; no new prompt/cron/feature-flag debt.
