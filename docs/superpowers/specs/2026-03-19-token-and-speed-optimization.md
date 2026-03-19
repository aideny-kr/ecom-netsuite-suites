# Token & Speed Optimization — Reduce Cost and Latency

**Date**: 2026-03-19
**Priority**: CRITICAL — $30+ daily test costs, 20-60s response times
**Goal**: First message < 30K tokens, follow-ups < 40K tokens, responses < 10s

---

## Problem

### Token bloat
- First message: ~28K input tokens (acceptable)
- Second message in same session: **103K input tokens** (3.7x increase)
- Root cause: tool results (file content, query results) from previous turns stay in message history verbatim. A single `workspace_read_file` adds 6K chars to every subsequent turn.
- With 4-5 turns in a session, tool result accumulation pushes input to 200K+

### Speed
- Simple data queries: 15-30s (should be < 5s)
- Script searches: 20-40s (should be < 10s)
- Change requests: 40-60s (should be < 20s)
- Root cause: multiple sequential LLM calls (each 5-10s), redundant file reads, large prompts increase time-to-first-token

---

## Token Fixes

### Fix 1: Condense tool results in message history (HIGH IMPACT)

**File**: `backend/app/services/chat/orchestrator.py` (history loading)

When loading conversation history, tool results from previous turns should be condensed. The current history loader already uses `content_summary` for older messages, but tool call payloads are stored in `tool_calls` JSON column and replayed in full.

**Implementation**: When building `history_messages` for the LLM, replace tool result content in older turns (beyond the last 2 messages) with a summary:

```python
# For messages older than KEEP_RECENT_FULL (last 2 assistant messages):
for tool_call in message.tool_calls:
    if tool_call.get("result_payload"):
        # Replace full result with summary
        tool_call["result_summary_only"] = True
        # "Returned 19 rows (columns: rma_number, rma_date, customer, status, location)"
        # instead of the full 19 rows of data
```

**Estimated savings**: 40-60K tokens per follow-up message.

### Fix 2: Cap workspace_read_file content in tool results

**File**: `backend/app/mcp/tools/workspace_tools.py`

`workspace_read_file` returns up to 6K chars of file content as the tool result. This content gets stored in the message and replayed in every subsequent turn.

**Implementation**: After the agent processes the file, replace the full content in the tool result with a condensed version:

```python
# In base_agent after tool execution, if tool is workspace_read_file:
if block.name == "workspace_read_file" and len(result_str) > 2000:
    # Keep first 500 chars + last 500 chars for context
    condensed = result_str[:500] + "\n... (truncated for history) ...\n" + result_str[-500:]
    # Store condensed version in the message for history replay
```

**Estimated savings**: 4-5K tokens per workspace_read_file call in history.

### Fix 3: Reduce KEEP_RECENT for workspace sessions

**File**: `backend/app/services/chat/orchestrator.py`

Currently `KEEP_RECENT = 8` (4 exchanges) get full content. For workspace sessions where each turn has large tool results, reduce to 4 (2 exchanges):

```python
keep_recent = 4 if workspace_context else 8
```

**Estimated savings**: 20-30K tokens for workspace sessions with 3+ turns.

---

## Speed Fixes

### Fix 4: Use Haiku/Flash for simple lookups (HIGH IMPACT)

**File**: `backend/app/services/chat/orchestrator.py`

Simple queries ("show me item FRANCR000B", "how many open POs") don't need Sonnet. Route simple lookups to Haiku (10x faster, 10x cheaper).

**Implementation**: After intent classification, if the query is a simple lookup (single entity, no joins, no analysis):

```python
# In orchestrator, after classify_importance:
if importance_tier == ImportanceTier.CASUAL and _is_simple_lookup(sanitized_input):
    model = "claude-haiku-4-5-20251001"  # Fast path
```

`_is_simple_lookup` heuristic: query mentions a specific ID/number, no "compare", "trend", "breakdown", "analysis" keywords.

**Estimated latency reduction**: 5-10s → 1-2s for simple lookups.

### Fix 5: Stream tool execution status

**File**: `frontend/src/components/chat/message-list.tsx`

The UI shows "Thinking..." for the entire duration. Show real-time tool status:
- "Searching scripts..." (workspace_search)
- "Reading file..." (workspace_read_file)
- "Running query..." (netsuite_suiteql)
- "Creating changeset..." (workspace_propose_patch)

This is already implemented server-side (`yield "tool_status", f"Executing {block.name}..."`). Verify the frontend renders it.

**Estimated perceived speed improvement**: 50% (user sees progress, not a blank spinner).

### Fix 6: Parallel entity resolution + context assembly

**File**: `backend/app/services/chat/orchestrator.py`

Currently sequential:
1. Entity resolution (Haiku LLM call ~1s)
2. Domain knowledge retrieval (embedding + DB ~500ms)
3. Proven patterns retrieval (embedding + DB ~500ms)
4. Schema selection (~10ms)

Entity resolution and domain knowledge retrieval are independent — run them in parallel with `asyncio.gather()`:

```python
entity_task = asyncio.create_task(resolver.resolve_entities(...))
dk_task = asyncio.create_task(retrieve_domain_knowledge(db, user_message))
pattern_task = asyncio.create_task(retrieve_similar_patterns(db, tenant_id, user_message))

vernacular, dk_results, patterns = await asyncio.gather(entity_task, dk_task, pattern_task)
```

**Estimated latency reduction**: 1-2s per message (overlapping 3 sequential calls).

### Fix 7: Skip context assembly for chitchat

**File**: `backend/app/services/chat/orchestrator.py`

The `_CHITCHAT_RE` regex already detects greetings/thanks. But the orchestrator still runs entity resolution, domain knowledge retrieval, and schema assembly for chitchat. Skip all of it:

```python
if _CHITCHAT_RE.match(sanitized_input):
    # Skip entity resolution, domain knowledge, proven patterns, schema
    # Just respond with the LLM directly — no tools needed
```

This is partially implemented but the entity resolver still runs. Verify and complete.

**Estimated savings**: 2-3s + 5K tokens for chitchat messages.

---

## Implementation Order

| # | Fix | Impact | Effort | Dependencies |
|---|-----|--------|--------|--------------|
| 1 | Condense tool results in history | 40-60K tokens saved | 1 hr | None |
| 6 | Parallel context assembly | 1-2s latency saved | 30 min | None |
| 4 | Haiku for simple lookups | 5-10s latency saved | 30 min | None |
| 2 | Cap workspace_read_file in history | 4-5K tokens saved | 15 min | None |
| 7 | Skip context for chitchat | 2-3s + 5K tokens | 15 min | None |
| 3 | Reduce KEEP_RECENT for workspace | 20-30K tokens saved | 5 min | Fix 1 |
| 5 | Stream tool status in UI | Perceived speed | 30 min | None |

---

## Success Criteria

| Metric | Current | Target |
|--------|---------|--------|
| First message input tokens | 28K | < 25K |
| Follow-up input tokens | 100-260K | < 40K |
| Simple lookup latency | 15-30s | < 5s |
| Data query latency | 15-30s | < 10s |
| Change request latency | 40-60s | < 20s |
| Cost per query (avg) | $0.29 | < $0.10 |
