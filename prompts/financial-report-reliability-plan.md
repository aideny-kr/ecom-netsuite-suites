# Financial Report Reliability — Comprehensive Fix Plan

## The Problem

The chat agent is unreliable at financial reporting. It was OK early on but has degraded. The diagnosis identified three issues:

1. **LLM doing arithmetic** — summing 78 rows produces different totals each run ($50,171 vs $47,574)
2. **Data format inconsistency** — SuiteQL returns list-of-lists but consumers expect list-of-dicts
3. **Financial mode task still forces SuiteQL** — despite MCP `ns_runReport` being available and producing pre-computed totals

## Root Cause Analysis

The orchestrator's financial mode (lines 607-631) hardcodes a **SuiteQL-only approach**: it injects TAL join patterns, BUILTIN.CONSOLIDATE syntax, column names, and GL accounting rules. This means:

- Every financial query goes through raw SuiteQL → LLM must compute aggregates from raw rows
- The 30+ line task augmentation eats tokens on every financial query
- The LLM is told to use `netsuite_suiteql` (local) and never mentions MCP `ns_runReport`
- Even though MCP tools are detected and listed in the tool inventory, the task augmentation overrides with explicit SuiteQL instructions

Meanwhile, MCP `ns_runReport` returns **pre-computed totals** with proper sign conventions, currency consolidation, and period filtering — exactly what the diagnosis says we need.

## The Fix: Three Layers

### Layer 1: MCP-First Financial Mode (Orchestrator)

**File: `backend/app/services/chat/orchestrator.py` lines 607-631**

Replace the current SuiteQL-focused financial mode task with MCP-first guidance:

```python
# CURRENT (lines 607-631): Forces SuiteQL TAL approach
if not _is_chitchat and is_financial:
    unified_task = (
        f"{sanitized_input}\n\n"
        f"[{_FINANCIAL_MODE_TAG}] Use TransactionAccountingLine (TAL) joined to Account ..."
        # ... 20+ lines of SuiteQL column names and join patterns
    )

# NEW: MCP-first, SuiteQL fallback
if not _is_chitchat and is_financial:
    # Check if MCP report tools are available for this tenant
    has_mcp_reports = "FINANCIAL_REPORTS" in ext_mcp_tools

    if has_mcp_reports:
        report_tool_name = ext_mcp_tools["FINANCIAL_REPORTS"]
        discovery_tool_name = ext_mcp_tools.get("REPORT_DISCOVERY", "")
        subsidiary_tool_name = ext_mcp_tools.get("SUBSIDIARIES", "")

        unified_task = (
            f"{sanitized_input}\n\n"
            f"[{_FINANCIAL_MODE_TAG}] FINANCIAL REPORT MODE — USE MCP TOOLS:\n\n"
            f"1. DISCOVER the report: Use `{discovery_tool_name}` to find the correct report ID.\n"
            f"   Common reports: Income Statement, Balance Sheet, Trial Balance, A/R Aging, A/P Aging, Cash Flow.\n"
            f"2. CHECK subsidiaries (if multi-subsidiary): Use `{subsidiary_tool_name}` to get subsidiary IDs.\n"
            f"3. RUN the report: Use `{report_tool_name}` with the discovered reportId, date range, and subsidiary.\n\n"
            f"CRITICAL RULES:\n"
            f"- NetSuite computes all totals, sign conventions, and currency consolidation natively.\n"
            f"- DO NOT recompute or re-sum any numbers from the report. Present them EXACTLY as returned.\n"
            f"- DO NOT use SuiteQL for standard financial statements. MCP reports are authoritative.\n"
            f"- If the report returns line items, present them in a formatted table.\n"
            f"- If the user asks for comparison (e.g., month-over-month), run the report twice with different periods.\n"
        )
    else:
        # Fallback: no MCP report tools available — use SuiteQL TAL approach
        unified_task = (
            f"{sanitized_input}\n\n"
            f"[{_FINANCIAL_MODE_TAG}] Use TransactionAccountingLine (TAL) joined to Account "
            # ... keep existing SuiteQL guidance as fallback
        )
    print("[UNIFIED] Financial report mode activated"
          f" ({'MCP' if has_mcp_reports else 'SuiteQL fallback'})", flush=True)
```

**Why this fixes the problem:**
- MCP `ns_runReport` returns pre-computed totals — the LLM never does arithmetic
- NetSuite handles sign conventions, multi-currency consolidation, and period filtering
- Eliminates 30+ lines of TAL/GL accounting instructions from the prompt (token savings)
- Falls back to SuiteQL only when MCP report tools aren't available

### Layer 2: Server-Side Computation for SuiteQL Path

When the SuiteQL fallback IS used (or for ad-hoc financial queries), the agent should NEVER let the LLM sum raw rows. Instead:

**File: `backend/app/services/chat/agents/unified_agent.py` — `<tool_selection>` section**

Add this rule to the existing SuiteQL guidance:

```
FOR FINANCIAL DATA VIA SUITEQL:
→ ALWAYS use SQL aggregates (SUM, COUNT, AVG) in the query itself. NEVER return raw rows for the LLM to sum.
→ WRONG: SELECT account, amount FROM tal ... (returns 78 rows for LLM to sum)
→ RIGHT: SELECT a.accttype, SUM(BUILTIN.CONSOLIDATE(...)) as total FROM tal ... GROUP BY a.accttype
→ The query result should contain the final numbers. Your job is to PRESENT them, not COMPUTE them.
→ For comparisons, use two separate queries or CASE WHEN for pivot-style output.
```

**File: `backend/app/services/chat/agents/unified_agent.py` — add to `<suiteql_dialect_rules>`**

```
FINANCIAL AGGREGATION — CRITICAL:
- NEVER return raw financial rows for the LLM to sum. Use SQL GROUP BY + SUM().
- WRONG: "Show me all revenue accounts" → returns 78 rows → LLM hallucinates total
- RIGHT: "Show me revenue by account type" → SUM(amount) GROUP BY accttype → 5 rows with pre-computed totals
- For net income: compute in SQL → `SUM(CASE WHEN accttype IN ('Income','OthIncome') THEN amount * -1 ELSE amount END)`
- The LLM should PRESENT numbers, never COMPUTE them. All math happens in SQL.
```

### Layer 3: Row Normalization for SuiteQL Results

**File: `backend/app/services/chat/tool_call_results.py` or new `backend/app/services/chat/result_normalizer.py`**

When SuiteQL returns list-of-lists, normalize to list-of-dicts before the agent processes them:

```python
def normalize_suiteql_result(result: dict) -> dict:
    """Normalize SuiteQL results to always use list-of-dicts format.

    SuiteQL can return:
    - {"columns": [...], "rows": [[...], ...]}  → list-of-lists
    - [{"col": "val", ...}, ...]                 → already list-of-dicts

    Always returns list-of-dicts for consistent downstream processing.
    """
    if isinstance(result, dict) and "columns" in result and "rows" in result:
        columns = result["columns"]
        rows = result["rows"]
        return {
            "items": [
                dict(zip(columns, row))
                for row in rows
            ],
            "totalResults": len(rows),
        }
    return result
```

Apply this in `execute_tool_call()` after every `netsuite_suiteql` call, before the result is sent back to the agent.

## Implementation Order

### Change 1: Financial mode MCP-first (orchestrator.py)
- Replace lines 607-631 with MCP-first branch + SuiteQL fallback
- This is the highest-impact change — eliminates LLM arithmetic for standard reports
- `ext_mcp_tools` dict is already built earlier in the function (line ~356)

### Change 2: Prompt caching (orchestrator.py) — SEPARATE TDD
- See `prompts/orchestrator-prompt-caching-tdd.md`
- Wire `split_system_prompt()` into the orchestrator's stream_message calls
- This cuts the token cost of every query by 40-50%

### Change 3: SuiteQL aggregation rule (unified_agent.py)
- Add the "never return raw rows" rule to `<tool_selection>` and `<suiteql_dialect_rules>`
- Small prompt change, big behavioral impact

### Change 4: Row normalization (tool_call_results.py)
- Add `normalize_suiteql_result()` function
- Apply after every `netsuite_suiteql` tool execution
- Prevents the list-of-lists vs list-of-dicts crash

## What NOT to Change

- `netsuite_report_service.py` — already correct, parses MCP report output properly
- `netsuite_report.py` tool wrapper — already has fallback to legacy
- `prompt_cache.py` — already complete
- `anthropic_adapter.py` — already handles cache_control

## Testing Scenarios

### MCP Path (primary)
1. "Show me the income statement for February 2026"
   → Agent calls ns_listAllReports → ns_runReport(-200, period)
   → Returns pre-computed totals → LLM presents as table
   → **Verify: same numbers every run**

2. "Balance sheet as of March 12, 2026"
   → Agent calls ns_runReport(-202, dateTo only)
   → **Verify: no dateFrom sent (inception-to-date)**

3. "Compare January vs February P&L"
   → Agent calls ns_runReport twice (Jan, Feb)
   → LLM presents side-by-side, computes variance from 2 pre-computed totals
   → **Verify: variance = Feb total - Jan total (simple subtraction, not 78-row sum)**

### SuiteQL Fallback Path
4. "What's our top 10 customers by revenue this quarter?"
   → Not a standard report → SuiteQL with GROUP BY + SUM
   → **Verify: query uses SUM(tl.amount * -1) GROUP BY, not raw rows**

5. "Revenue by account for February"
   → Agent might use MCP report OR SuiteQL depending on context
   → If SuiteQL: **verify GROUP BY + SUM in query, not raw row dump**

### Edge Cases
6. No MCP connector available → falls back to SuiteQL TAL approach
7. MCP report fails (timeout) → falls back to SuiteQL
8. User asks for non-standard report (e.g., "revenue by SKU") → correctly routes to SuiteQL, not MCP report

## Expected Impact

| Metric | Before | After |
|--------|--------|-------|
| Financial report accuracy | ~60-70% (LLM arithmetic varies) | ~99% (NetSuite pre-computes) |
| Financial query tokens | ~40-50K (TAL instructions + schema + rows) | ~2-5K (MCP tool call + formatted result) |
| Total API cost (12-day) | $103 | ~$40-50 (with prompt caching) |
| Cache hit ratio | 5% | 60-80% (with prompt caching fix) |

## Files Changed

| File | Change | Impact |
|------|--------|--------|
| `backend/app/services/chat/orchestrator.py` L607-631 | MCP-first financial mode + SuiteQL fallback | Eliminates LLM arithmetic |
| `backend/app/services/chat/orchestrator.py` (caching) | Wire split_system_prompt → stream_message | 40-50% cost reduction |
| `backend/app/services/chat/agents/unified_agent.py` | Add aggregation rules to <tool_selection> + <suiteql_dialect_rules> | Prevents raw row dumps |
| `backend/app/services/chat/tool_call_results.py` | Add normalize_suiteql_result() | Fixes list-of-lists crash |

## Reference Skills
- `skills/netsuite-mcp/SKILL.md` — MCP tool reference and hybrid decision tree
- `prompts/orchestrator-prompt-caching-tdd.md` — Separate TDD for prompt caching fix
