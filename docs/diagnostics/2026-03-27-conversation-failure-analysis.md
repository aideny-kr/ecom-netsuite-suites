# Conversation Failure Analysis — 2026-03-27

## Session Summary

User started with BigQuery B2B customer data, then wanted to enrich customer names using web search. The conversation degraded over ~10 messages with the agent unable to fulfill the user's intent.

## Message-by-Message Trace

| # | User Intent | Agent Routing | What Agent Did | Problem |
|---|-------------|---------------|---------------|---------|
| 1-12 | (earlier BigQuery queries — working fine) | bi-agent (session pinned) | BigQuery queries ✓ | None |
| 13 | "let's pull 1000 list if we can" | Tier 3 → unified-agent | Ran 2 NetSuite SuiteQL queries, pulled B2B customers ✓ | **Routing break**: was on bi-agent, now unified. Session lost BigQuery context. |
| 14 | "Want the full 1,000 and fill in customer name with best effort" | Tier 3 → unified-agent | Ran SuiteQL but `enforce_limit` capped at 100 rows | **Row limit**: user asked for 1000 but got 100. Agent didn't explain the cap. |
| 15 | "let's not do that. So we have the list of Business Order we pulled previously. Can you cross reference..." | Tier 2 → **bi-agent** | Unknown (BI agent has no NetSuite tools) | **Wrong agent**: user referenced earlier data, semantic router misclassified as BI query. BI agent can't do customer lookups. |
| 16 | "you should now search web" | Tier 3 → unified-agent | Ran 3 MORE NetSuite SuiteQL queries | **Ignored explicit instruction**: user literally said "search web" but agent ran DB queries. `web_search` tool available but unused. |
| 17 | "run the merged report" | Tier 3 → unified-agent (investigation mode) | Ran 7 NetSuite SuiteQL queries | **Over-engineering**: agent ran 7 queries trying to build a merged report instead of using cached results + web enrichment. 167K tokens consumed. |

## Root Causes

### 1. Agent Routing Instability (Critical)

The conversation bounced between 3 agents across 5 messages:
```
bi-agent → unified-agent → unified-agent → bi-agent → unified-agent → unified-agent
```

**Why**: Session pinning (`_infer_previous_agent`) only works when the previous assistant message has identifiable tool calls. When the user changes topics mid-conversation (BigQuery → NetSuite → web search), the routing system treats each message independently. There's no concept of "the user is in a multi-step workflow that spans data sources."

**Impact**: Each agent switch loses context. The BI agent doesn't know about the NetSuite data the unified agent pulled. The unified agent doesn't know about the BigQuery results from earlier.

### 2. Web Search Tool Available But Never Used (Critical)

The unified agent had `web_search` in its tool list but chose NetSuite SuiteQL queries instead — even when the user explicitly said "you should now search web."

**Why**:
- The DATA FRESHNESS RULES in the prompt say "If the user asks a data question, you MUST call a tool to get fresh data." The agent interprets "fill in customer name" as a data question and reaches for SuiteQL.
- The agent's training bias favors structured database queries over web search for entity enrichment.
- There's no prompt instruction that says "when the user explicitly asks for web search, use the web_search tool."

### 3. History Condensation Destroys Cross-Query Context (Important)

By message 14, `build_condensed_history()` had already summarized the earlier BigQuery results into one-line summaries. When the user said "the list of Business Orders we pulled previously," the agent couldn't access the actual data — it only saw a summary like "BigQuery query returned 1000 rows."

**Why**: The 30-row preview is only in the data_table SSE event (sent to frontend). The LLM context gets a 5-30 row condensed version. By the next turn, even that is summarized to "data table with N rows." The actual data is gone from the LLM's context.

**Impact**: The agent can't "use the same result" because it literally doesn't have the result anymore. It re-queries instead, which is what the user explicitly didn't want.

### 4. Row Limit Enforcement Without Explanation (Important)

The user asked for 1,000 rows. The local SuiteQL tool has `enforce_limit(max_rows=100)` that silently caps the result. The agent didn't tell the user it could only return 100 rows via the local tool, or suggest using the external MCP which supports larger result sets.

**Why**: `enforce_limit` is a safety mechanism but it operates silently. The agent sees 100 rows come back and presents them as if that's the full result.

### 5. Token Consumption Spiral (Important)

The final "run the merged report" message triggered investigation mode (context_need=FULL) and consumed 167,947 cached tokens + 7 SuiteQL queries. This is because:
- History was 20 messages (6 summarized) — each with large tool results
- Investigation mode sends full row data to the LLM
- The agent kept querying variations trying to build the "perfect" list

**Cost impact**: This single message likely cost $1-2 in API tokens.

## Recommendations

### R1: Explicit Tool Override (Quick Win)

When the user says "search web", "use web search", "look this up online" — the orchestrator should detect this and force `web_search` as the first tool call. Similar to how `is_financial` forces the UnifiedAgent for income statements.

### R2: Cross-Source Workflow Memory (Medium)

When the user references "the list we pulled earlier," the system needs to retrieve the cached result (from Redis result cache or structured_output) and inject it into the current context. The follow-up intelligence system we built (result cache + reference_previous_result) should handle this, but it's not being triggered because:
- The TRANSFORM classifier doesn't match "fill in the customer name" (it's not chart/pivot/export)
- The agent doesn't know to call `reference_previous_result`

### R3: Agent Handoff Protocol (Medium)

When a specialized agent (BI) can't fulfill a request (needs NetSuite data or web search), it should explicitly hand off to the unified agent with context: "The user wants to enrich BigQuery results with web data. Here are the results so far: [cached data reference]."

Currently, the routing just picks a different agent with no context transfer.

### R4: Transparent Row Limits (Quick Win)

When `enforce_limit` caps results below the user's requested count, the condensed result should include: "Note: result capped at 100 rows. The full 1000-row result was sent to the frontend for download. Use Export CSV for the complete dataset."

### R5: Reduce Re-Query Bias (Medium)

The DATA FRESHNESS RULES are too aggressive. Add a rule: "If the user references data already in this conversation ('the list we pulled', 'use the same result', 'from earlier'), check the result cache first. Do NOT re-query unless the user explicitly asks for fresh data."

## Priority

| Fix | Impact | Effort | Priority |
|-----|--------|--------|----------|
| R1: Explicit web search override | High | Low | P0 |
| R4: Transparent row limits | Medium | Low | P0 |
| R5: Reduce re-query bias | High | Medium | P1 |
| R2: Cross-source workflow memory | High | High | P1 |
| R3: Agent handoff protocol | High | High | P2 |
