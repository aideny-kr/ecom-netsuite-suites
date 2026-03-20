# Pivot Query Result Tool — Design Spec

**Date**: 2026-03-20
**Priority**: HIGH — pivot queries are the #1 quality gap in the chat agent
**Goal**: Deterministic server-side pivoting, no LLM value selection

---

## Problem

The LLM cannot reliably build CASE WHEN pivot queries. It:
- Drops variants ("Lotus - Refurbished" → merged into "Lotus")
- Adds non-existent values (Bamboo, Carrot with zero data)
- Hallucates cross-column mappings ("Azalea = Laptop")
- Ignores prompt instructions about value lists

The flat GROUP BY query works perfectly every time. The pivot formatting is where it breaks.

## Solution

New tool `pivot_query_result` that the agent calls instead of constructing pivot SQL. The agent provides the original query + field mapping. The tool re-executes the query (without row limit), pivots in Python, and returns a structured table.

---

## Tool Definition

```
Name: pivot_query_result
Description: Pivot a SuiteQL query result into a crosstab table. Re-executes
  the query without row limits and pivots server-side. Use this instead of
  building CASE WHEN pivot SQL manually.

Parameters:
  query: str (required)
    The original SuiteQL query to re-execute. FETCH FIRST will be stripped.

  row_field: str (required)
    Column name for row grouping (e.g., "week_start_date")

  column_field: str (required)
    Column name whose distinct values become pivot columns (e.g., "platform")

  value_field: str (required)
    Column name to aggregate into cells (e.g., "total_qty")

  aggregation: str (optional, default "sum")
    Aggregation function: "sum", "count", "avg", "max", "min"

  include_total: bool (optional, default true)
    Add a "Total" column summing all pivot columns per row
```

## Architecture

### Backend: `backend/app/services/chat/tools.py`

Register `pivot_query_result` in the tool registry. Implementation in `backend/app/services/pivot_service.py`.

### Pivot Service: `backend/app/services/pivot_service.py` (NEW)

```python
async def pivot_query_result(
    query: str,
    row_field: str,
    column_field: str,
    value_field: str,
    aggregation: str = "sum",
    include_total: bool = True,
    tenant_id: UUID,
    actor_id: UUID,
    db: AsyncSession,
) -> dict:
    """
    1. Strip FETCH FIRST / ROWNUM limits from query
    2. Re-execute via get_valid_token() + execute_suiteql_via_rest()
    3. Parse result rows
    4. Build pivot: {row_key: {col_value: aggregated_value}}
    5. Return {"columns": [row_field, col1, col2, ..., "Total"], "rows": [...]}
    """
```

### Pivot Logic (no pandas)

```python
def _pivot_rows(
    columns: list[str],
    rows: list[list],
    row_field: str,
    column_field: str,
    value_field: str,
    aggregation: str,
    include_total: bool,
) -> tuple[list[str], list[list]]:
    # 1. Find column indices
    row_idx = columns.index(row_field)
    col_idx = columns.index(column_field)
    val_idx = columns.index(value_field)

    # 2. Collect distinct column values (ordered by first appearance)
    seen_cols = dict()  # preserves order
    for row in rows:
        val = str(row[col_idx])
        if val not in seen_cols:
            seen_cols[val] = None
    pivot_cols = list(seen_cols.keys())

    # 3. Build pivot dict: {row_key: {col_value: [values]}}
    pivot = defaultdict(lambda: defaultdict(list))
    row_order = dict()
    for row in rows:
        rk = str(row[row_idx])
        ck = str(row[col_idx])
        try:
            v = float(row[val_idx]) if row[val_idx] is not None else 0
        except (ValueError, TypeError):
            v = 0
        pivot[rk][ck].append(v)
        if rk not in row_order:
            row_order[rk] = None

    # 4. Aggregate
    agg_fn = {"sum": sum, "count": len, "avg": lambda x: sum(x)/len(x) if x else 0,
              "max": max, "min": min}.get(aggregation, sum)

    # 5. Build output
    out_columns = [row_field] + pivot_cols + (["Total"] if include_total else [])
    out_rows = []
    for rk in row_order:
        row = [rk]
        total = 0
        for pc in pivot_cols:
            val = agg_fn(pivot[rk][pc]) if pivot[rk][pc] else 0
            row.append(val)
            total += val if isinstance(val, (int, float)) else 0
        if include_total:
            row.append(total)
        out_rows.append(row)

    return out_columns, out_rows
```

### Key behaviors:
- **Only values that exist in the data become columns** — no hallucinated values
- **All variants preserved** — "Lotus" and "Lotus - Refurbished" are separate columns
- **Zero-data columns never appear** — if Bamboo has no rows, no Bamboo column
- **Column order** — by first appearance in the data (usually matches the query ORDER BY)
- **Row limit** — re-executes with up to 10,000 rows (same as export)

### Tool Registration

Add to `_UNIFIED_TOOL_NAMES` in unified_agent.py and to `build_local_tool_definitions()` in tools.py.

### Agent Prompt Addition

Add to `<tool_selection>` in unified_agent.py:

```
PIVOT / CROSSTAB:
→ Do NOT build CASE WHEN pivot SQL manually — use pivot_query_result tool.
→ First run a flat GROUP BY query, then call pivot_query_result with the same query.
→ The tool re-executes the query and pivots server-side with exact database values.
```

---

## What the agent decides vs what the code decides

| Decision | Agent (LLM) | Code (deterministic) |
|----------|-------------|---------------------|
| Which query to run | ✅ | |
| Row field name | ✅ | |
| Column field name | ✅ | |
| Value field name | ✅ | |
| Which values become columns | | ✅ (from data) |
| How to aggregate | ✅ (sum/count/avg) | |
| Column ordering | | ✅ (from data) |
| Include total | ✅ | |

---

## Testing

### Unit tests (`backend/tests/test_pivot_service.py`)
- Pivot 3 platforms × 4 weeks → correct columns and values
- Refurbished variants preserved as separate columns
- Zero-data values excluded (no Bamboo column if no Bamboo rows)
- Empty result → empty pivot
- Missing field name → clear error
- Aggregation modes: sum, count, avg

### Integration test
- Run real SuiteQL query → pivot → verify columns match distinct values in data

---

## Files to create/modify

| File | Action |
|------|--------|
| `backend/app/services/pivot_service.py` | NEW — pivot logic |
| `backend/app/services/chat/tools.py` | ADD tool definition + execution |
| `backend/app/services/chat/agents/unified_agent.py` | ADD to tool names + prompt |
| `backend/tests/test_pivot_service.py` | NEW — unit tests |
