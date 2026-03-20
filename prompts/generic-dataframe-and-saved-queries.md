# Generic DataFrame & Saved Queries Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render all SuiteQL query results in a rich DataFrame component (not just financial reports), and let users save any query output as a reusable saved query from the DataFrame UI.

**Architecture:** Extend the existing `_intercept_financial_report` pattern to also intercept `netsuite_suiteql` tool results via a unified `_intercept_tool_result` function. The SSE event carries full tabular data to the frontend, where a new generic `<DataFrameTable />` component renders it. The existing `SavedSuiteQLQuery` model and `/api/v1/skills` CRUD endpoints are reused — a "Save Query" button in the DataFrame passes the query text and a user-provided name.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Pydantic v2, Next.js 14, React, TanStack React Query, Tailwind CSS, shadcn/ui, pytest, Jest

---

## Scope

Two features, built sequentially:

1. **Feature A — Generic DataFrame**: Intercept SuiteQL tool results, emit SSE `data_table` event, render `<DataFrameTable />` component for all query outputs
2. **Feature B — Save from DataFrame**: Add "Save Query" button to `<DataFrameTable />`, wire to existing saved query API

These share the SSE + component infrastructure, so they're in one plan.

---

## File Structure

### Backend (Feature A)

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/app/services/chat/orchestrator.py` | Modify | Rename `_intercept_financial_report` → `_intercept_tool_result`, add SuiteQL interception |
| `backend/app/services/chat/agents/base_agent.py` | No change | Already has `tool_result_interceptor` callback |
| `backend/app/services/chat/agents/unified_agent.py` | No change | Already passes through `tool_result_interceptor` |
| `backend/tests/test_orchestrator_financial_sse.py` | Modify → Rename to `test_tool_result_interception.py` | Tests for both financial + SuiteQL interception |

### Frontend (Feature A)

| File | Action | Responsibility |
|------|--------|---------------|
| `frontend/src/components/chat/data-frame-table.tsx` | Create | Generic table component for any columnar data |
| `frontend/src/lib/chat-stream.ts` | Modify | Add `data_table` SSE event type + `DataTableData` interface |
| `frontend/src/app/(dashboard)/chat/page.tsx` | Modify | Add `dataTable` state + `dataTablesRef` alongside existing financial report state |
| `frontend/src/components/chat/message-list.tsx` | Modify | Render `<DataFrameTable />` for SuiteQL results (streaming + persisted) |

### Frontend (Feature B)

| File | Action | Responsibility |
|------|--------|---------------|
| `frontend/src/components/chat/data-frame-table.tsx` | Modify | Add "Save Query" button using existing `useCreateSavedQuery` hook |

No backend changes for Feature B — the `/api/v1/skills` CRUD endpoints already exist.

---

## Chunk 1: Backend — Unified Tool Result Interception

### Task 1: Extend interception to SuiteQL results

**Context:** Currently `_intercept_financial_report()` only handles `netsuite.financial_report` / `netsuite_financial_report`. We need to also intercept `netsuite_suiteql` / `netsuite.suiteql` results and emit a `data_table` SSE event. The SuiteQL result format differs: it has `columns` (string array) and `rows` (list-of-lists), whereas financial reports have `items` (list-of-dicts).

**Files:**
- Modify: `backend/tests/test_orchestrator_financial_sse.py` → rename to `backend/tests/test_tool_result_interception.py`
- Modify: `backend/app/services/chat/orchestrator.py`

- [ ] **Step 1: Write failing tests for SuiteQL interception**

Create new test class in `backend/tests/test_tool_result_interception.py` (rename existing file):

```python
"""Tests for _intercept_tool_result() in orchestrator.py."""

import json
import pytest

from app.services.chat.orchestrator import _intercept_tool_result


# -- Fixtures --

SAMPLE_FINANCIAL_RESULT = {
    "success": True,
    "report_type": "income_statement",
    "period": "Feb 2026",
    "columns": ["Account", "Amount"],
    "items": [
        {"account": "Revenue", "amount": 100000},
        {"account": "COGS", "amount": -40000},
        {"account": "Net Income", "amount": 60000},
    ],
    "summary": {"total_revenue": 100000, "net_income": 60000},
}

SAMPLE_SUITEQL_RESULT = {
    "columns": ["tranid", "entity", "amount", "status"],
    "rows": [
        ["SO-1001", "Acme Corp", 5000.00, "Pending"],
        ["SO-1002", "Globex Inc", 3200.50, "Billed"],
        ["SO-1003", "Initech", 1500.00, "Pending"],
    ],
    "row_count": 3,
    "truncated": False,
    "query": "SELECT tranid, entity, amount, status FROM transaction WHERE type = 'SalesOrd'",
    "limit": 1000,
}


def _result_str(data: dict) -> str:
    return json.dumps(data, default=str)


# -- Financial report tests (existing, updated function name) --

class TestInterceptFinancialReport:
    """Financial report interception — same behavior as before."""

    def test_success(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite.financial_report", result_str
        )
        assert event_type == "financial_report"
        assert sse_event is not None
        assert sse_event["report_type"] == "income_statement"
        assert sse_event["rows"] == SAMPLE_FINANCIAL_RESULT["items"]
        parsed = json.loads(condensed)
        assert parsed["success"] is True
        assert "items" not in parsed
        assert "rows" not in parsed
        assert parsed["total_rows"] == 3

    def test_underscore_tool_name(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_financial_report", result_str
        )
        assert event_type == "financial_report"
        assert sse_event is not None

    def test_failure_is_noop(self):
        failed = {"success": False, "error": "Query failed"}
        result_str = _result_str(failed)
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite.financial_report", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str


# -- SuiteQL data_table tests (NEW) --

class TestInterceptSuiteQL:
    """SuiteQL query results should emit data_table SSE event."""

    def test_suiteql_success(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "entity", "amount", "status"]
        assert sse_event["rows"] == SAMPLE_SUITEQL_RESULT["rows"]
        assert sse_event["row_count"] == 3
        assert sse_event["query"] == SAMPLE_SUITEQL_RESULT["query"]

    def test_suiteql_dot_name(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite.suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None

    def test_condensed_has_no_rows(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        parsed = json.loads(condensed)
        assert "rows" not in parsed
        assert parsed["row_count"] == 3
        assert "note" in parsed

    def test_condensed_preserves_columns(self):
        """LLM should know the columns to provide meaningful commentary."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        parsed = json.loads(condensed)
        assert parsed["columns"] == ["tranid", "entity", "amount", "status"]

    def test_suiteql_error_is_noop(self):
        error_result = {"error": True, "message": "Invalid column name"}
        result_str = _result_str(error_result)
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_suiteql_empty_rows(self):
        """Empty results should still emit data_table (shows 'no data' in UI)."""
        empty_result = {
            "columns": ["tranid"],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "query": "SELECT tranid FROM transaction WHERE 1=0",
            "limit": 1000,
        }
        result_str = _result_str(empty_result)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["rows"] == []
        assert sse_event["row_count"] == 0

    def test_suiteql_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", "Not JSON"
        )
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"


class TestInterceptNonMatchingTool:
    """Non-data tools should be untouched."""

    def test_rag_search_is_noop(self):
        result_str = _result_str({"chunks": [{"text": "hello"}]})
        event_type, sse_event, returned = _intercept_tool_result(
            "rag_search", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_tool_result_interception.py -v`
Expected: FAIL — `ImportError: cannot import name '_intercept_tool_result'`

- [ ] **Step 3: Implement `_intercept_tool_result`**

Replace `_intercept_financial_report` in `backend/app/services/chat/orchestrator.py`:

```python
# Return type: (event_type | None, sse_event_data | None, result_str)
# - event_type: "financial_report" or "data_table" or None
# - sse_event_data: dict for SSE event or None
# - result_str: condensed (if intercepted) or original

_FINANCIAL_TOOLS = frozenset({"netsuite.financial_report", "netsuite_financial_report"})
_SUITEQL_TOOLS = frozenset({"netsuite.suiteql", "netsuite_suiteql"})


def _intercept_tool_result(
    tool_name: str, result_str: str
) -> tuple[str | None, dict | None, str]:
    """Intercept data-producing tool results for frontend DataFrame rendering.

    Returns ``(event_type, sse_event_data, result_str_for_llm)``.
    - Financial reports → ``("financial_report", {...}, condensed)``
    - SuiteQL queries  → ``("data_table", {...}, condensed)``
    - Everything else  → ``(None, None, original_result_str)``
    """

    # --- Financial report path (existing) ---
    if tool_name in _FINANCIAL_TOOLS:
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None, None, result_str
        if not parsed.get("success"):
            return None, None, result_str

        rows = parsed.get("items", [])
        sse_event_data = {
            "report_type": parsed.get("report_type"),
            "period": parsed.get("period"),
            "columns": parsed.get("columns", []),
            "rows": rows,
            "summary": parsed.get("summary"),
        }
        condensed = json.dumps(
            {
                "success": True,
                "report_type": parsed.get("report_type"),
                "period": parsed.get("period"),
                "total_rows": len(rows),
                "summary": parsed.get("summary"),
                "note": (
                    "The full table has been sent to the frontend for rendering. "
                    "Do NOT rebuild or reproduce the table in your response. "
                    "Provide commentary and analysis only."
                ),
            },
            default=str,
        )
        return "financial_report", sse_event_data, condensed

    # --- SuiteQL query path (new) ---
    if tool_name in _SUITEQL_TOOLS:
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None, None, result_str

        # Error results pass through
        if isinstance(parsed, dict) and (parsed.get("error") is True or isinstance(parsed.get("error"), str)):
            return None, None, result_str

        columns = parsed.get("columns")
        rows = parsed.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return None, None, result_str

        row_count = parsed.get("row_count", len(rows))
        query = parsed.get("query", "")
        truncated = parsed.get("truncated", False)

        sse_event_data = {
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "query": query,
            "truncated": truncated,
        }

        condensed = json.dumps(
            {
                "columns": columns,
                "row_count": row_count,
                "truncated": truncated,
                "note": (
                    "The full data table has been sent to the frontend for rendering. "
                    "Do NOT rebuild or reproduce the table in your response. "
                    "Provide commentary, insights, and analysis only."
                ),
            },
            default=str,
        )
        return "data_table", sse_event_data, condensed

    # --- Not a data tool ---
    return None, None, result_str
```

- [ ] **Step 4: Update all callers of `_intercept_financial_report`**

In `orchestrator.py`, find and replace every call site:

**Raw agentic loop** (around line 1169):
```python
# OLD:
fin_sse, result_str = _intercept_financial_report(block.name, result_str)
if fin_sse is not None:
    yield {"type": "financial_report", "data": fin_sse}

# NEW:
intercept_type, intercept_data, result_str = _intercept_tool_result(block.name, result_str)
if intercept_type is not None:
    yield {"type": intercept_type, "data": intercept_data}
```

**Unified agent call** (around line 775): The `tool_result_interceptor` callback signature changes. Update the lambda/reference passed:

The `tool_result_interceptor` in `base_agent.py` expects `(tool_name, result_str) -> (dict | None, str)`. We need to update this to return `(tuple | None, str)` where the tuple is `(event_type, event_data)`.

Update `base_agent.py` interceptor type and logic:

```python
# base_agent.py — updated interceptor type
# Callback: (tool_name, result_str) -> ((event_type, event_data) | None, result_str_for_llm)
tool_result_interceptor: Callable[[str, str], tuple[tuple[str, dict] | None, str]] | None = None,

# In the tool execution section:
if tool_result_interceptor is not None:
    intercept_info, llm_result_str = tool_result_interceptor(
        block.name, result_str
    )
    if intercept_info is not None:
        yield "tool_intercept", intercept_info
```

Create a wrapper in `orchestrator.py` that adapts `_intercept_tool_result` to the interceptor signature:

```python
def _tool_interceptor(tool_name: str, result_str: str) -> tuple[tuple[str, dict] | None, str]:
    """Adapter: wraps _intercept_tool_result for the agent callback interface."""
    event_type, event_data, new_result_str = _intercept_tool_result(tool_name, result_str)
    if event_type is not None and event_data is not None:
        return (event_type, event_data), new_result_str
    return None, new_result_str
```

Pass `_tool_interceptor` to `run_streaming`:
```python
tool_result_interceptor=_tool_interceptor,
```

Handle in orchestrator streaming loop:
```python
elif event_type == "tool_intercept":
    # payload is (event_type_str, event_data_dict)
    yield {"type": payload[0], "data": payload[1]}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_tool_result_interception.py -v`
Expected: All tests PASS

- [ ] **Step 6: Run full agent test suite to verify no regressions**

Run: `backend/.venv/bin/python -m pytest backend/tests/ -k "agent or orchestrator or financial" -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add backend/tests/test_tool_result_interception.py backend/app/services/chat/orchestrator.py backend/app/services/chat/agents/base_agent.py backend/app/services/chat/agents/unified_agent.py
git rm backend/tests/test_orchestrator_financial_sse.py 2>/dev/null || true
git commit -m "feat: extend tool result interception to SuiteQL queries (data_table SSE event)"
```

---

## Chunk 2: Frontend — DataFrameTable Component

### Task 2: Add `data_table` SSE event type to chat stream

**Context:** The frontend SSE parser (`chat-stream.ts`) needs to handle the new `data_table` event type alongside the existing `financial_report` type. The `data_table` event carries columns (string[]) and rows (unknown[][]) — list-of-lists format from SuiteQL.

**Files:**
- Modify: `frontend/src/lib/chat-stream.ts`

- [ ] **Step 1: Add `DataTableData` interface and event type**

In `frontend/src/lib/chat-stream.ts`, add alongside `FinancialReportData`:

```typescript
export interface DataTableData {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  query: string;
  truncated: boolean;
}
```

Add to `ChatStreamEvent` union:
```typescript
| { type: "data_table"; data: DataTableData }
```

Add to `StreamHandlers`:
```typescript
onDataTable?: (data: DataTableData) => void;
```

- [ ] **Step 2: Handle `data_table` in `normalizeStreamEvent` and `consumeChatStream`**

In `normalizeStreamEvent`, add case for `"data_table"`:
```typescript
case "data_table": {
  const tableData = typeof raw.data === "string" ? JSON.parse(raw.data) : raw.data;
  return { type: "data_table", data: tableData as DataTableData };
}
```

In `consumeChatStream`, add dispatch:
```typescript
case "data_table":
  handlers.onDataTable?.(event.data);
  break;
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/chat-stream.ts
git commit -m "feat: add data_table SSE event type to chat stream parser"
```

### Task 3: Create `<DataFrameTable />` component

**Context:** This is a generic tabular data component for SuiteQL query results. Unlike `<FinancialReport />` (which has sections, accounting formatting, computed rows), this is a flat table with sortable columns, number formatting, and export buttons. It receives list-of-lists data (columns + rows arrays).

**Files:**
- Create: `frontend/src/components/chat/data-frame-table.tsx`

- [ ] **Step 1: Create the component**

```typescript
"use client";

import { useState, useMemo, useCallback } from "react";
import { cn } from "@/lib/utils";
import type { DataTableData } from "@/lib/chat-stream";
import {
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Copy,
  Check,
  Download,
} from "lucide-react";
import {
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface DataFrameTableProps {
  data: DataTableData;
  queryText?: string;
  onSaveQuery?: (queryText: string) => void;
}

type SortDirection = "asc" | "desc" | null;

export function DataFrameTable({ data, queryText, onSaveQuery }: DataFrameTableProps) {
  const { columns, rows, row_count, truncated } = data;
  const [sortCol, setSortCol] = useState<number | null>(null);
  const [sortDir, setSortDir] = useState<SortDirection>(null);
  const [copied, setCopied] = useState(false);

  const handleSort = useCallback(
    (colIndex: number) => {
      if (sortCol === colIndex) {
        setSortDir((d) => (d === "asc" ? "desc" : d === "desc" ? null : "asc"));
        if (sortDir === "desc") setSortCol(null);
      } else {
        setSortCol(colIndex);
        setSortDir("asc");
      }
    },
    [sortCol, sortDir],
  );

  const sortedRows = useMemo(() => {
    if (sortCol === null || sortDir === null) return rows;
    return [...rows].sort((a, b) => {
      const aVal = a[sortCol];
      const bVal = b[sortCol];
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      const aNum = typeof aVal === "number" ? aVal : Number(aVal);
      const bNum = typeof bVal === "number" ? bVal : Number(bVal);
      if (!isNaN(aNum) && !isNaN(bNum)) {
        return sortDir === "asc" ? aNum - bNum : bNum - aNum;
      }
      const aStr = String(aVal);
      const bStr = String(bVal);
      return sortDir === "asc" ? aStr.localeCompare(bStr) : bStr.localeCompare(aStr);
    });
  }, [rows, sortCol, sortDir]);

  const handleCopy = useCallback(() => {
    const header = columns.join("\t");
    const body = sortedRows.map((row) => row.map((v) => v ?? "").join("\t")).join("\n");
    navigator.clipboard.writeText(`${header}\n${body}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [columns, sortedRows]);

  const handleDownloadCSV = useCallback(() => {
    const escape = (v: unknown) => {
      const s = String(v ?? "");
      return s.includes(",") || s.includes('"') || s.includes("\n")
        ? `"${s.replace(/"/g, '""')}"`
        : s;
    };
    const header = columns.map(escape).join(",");
    const body = rows.map((row) => row.map(escape).join(",")).join("\n");
    const blob = new Blob([`${header}\n${body}`], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `query-results-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [columns, rows]);

  if (columns.length === 0) return null;

  return (
    <div className="my-3 overflow-hidden rounded-xl border bg-card shadow-soft">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10">
            <svg className="h-3.5 w-3.5 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3 10h18M3 14h18M3 6h18M3 18h18" />
            </svg>
          </div>
          <div>
            <p className="text-[13px] font-semibold text-foreground">Query Results</p>
            <p className="text-[11px] text-muted-foreground">
              {row_count} row{row_count !== 1 ? "s" : ""}
              {truncated ? " (truncated)" : ""} · {columns.length} column{columns.length !== 1 ? "s" : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Copy to clipboard (tab-separated for Excel)"
          >
            {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            onClick={handleDownloadCSV}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Download as CSV"
          >
            <Download className="h-3 w-3" />
            CSV
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="max-h-[600px] overflow-auto scrollbar-thin">
        <table className="w-max min-w-full caption-bottom text-sm">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              {columns.map((col, i) => (
                <TableHead
                  key={col}
                  className="sticky top-0 z-10 h-auto cursor-pointer select-none whitespace-nowrap bg-muted/80 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur transition-colors hover:text-foreground"
                  onClick={() => handleSort(i)}
                >
                  <span className="inline-flex items-center gap-1">
                    {toReadableHeader(col)}
                    {sortCol === i && sortDir === "asc" && <ArrowUp className="h-3 w-3" />}
                    {sortCol === i && sortDir === "desc" && <ArrowDown className="h-3 w-3" />}
                    {sortCol !== i && <ArrowUpDown className="h-2.5 w-2.5 opacity-30" />}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedRows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="py-8 text-center text-[13px] text-muted-foreground">
                  No results
                </TableCell>
              </TableRow>
            ) : (
              sortedRows.map((row, ri) => (
                <TableRow key={ri} className="border-b border-border/50">
                  {row.map((cell, ci) => (
                    <TableCell
                      key={`${ri}-${ci}`}
                      className={cn(
                        "max-w-[320px] whitespace-nowrap px-3 py-2 text-[12px]",
                        isNumeric(cell) && "text-right tabular-nums",
                      )}
                    >
                      {formatCellValue(cell)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </table>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t px-4 py-2">
        <p className="text-[11px] text-muted-foreground">
          {truncated
            ? `Showing ${rows.length} of ${row_count} rows`
            : `${row_count} row${row_count === 1 ? "" : "s"} returned`}
        </p>
        {queryText && onSaveQuery && (
          <SaveQueryButton queryText={queryText} onSave={onSaveQuery} />
        )}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Save Query button (stub — wired in Task 5)
// ---------------------------------------------------------------------------

function SaveQueryButton({
  queryText,
  onSave,
}: {
  queryText: string;
  onSave: (queryText: string) => void;
}) {
  // Placeholder — will be implemented in Task 5
  return null;
}


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toReadableHeader(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function isNumeric(value: unknown): boolean {
  if (typeof value === "number") return true;
  if (typeof value === "string" && /^-?\d+\.?\d*([eE][+-]?\d+)?$/.test(value)) return true;
  return false;
}

function formatCellValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  if (typeof value === "string") {
    if (/^-?\d+\.?\d*([eE][+-]?\d+)?$/.test(value) && value.length > 0) {
      const num = Number(value);
      if (!isNaN(num)) {
        if (Number.isInteger(num)) return num.toLocaleString();
        return num.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        });
      }
    }
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/chat/data-frame-table.tsx
git commit -m "feat: create DataFrameTable component for generic query results"
```

### Task 4: Wire DataFrameTable into chat page and message list

**Context:** Follow the exact same pattern used for `financialReport` state. Add `dataTable` state + `dataTablesRef` for persistence across message refetches. During streaming, show `<DataFrameTable />` above the streaming text. For persisted messages, look up by message ID.

**Files:**
- Modify: `frontend/src/app/(dashboard)/chat/page.tsx`
- Modify: `frontend/src/components/chat/message-list.tsx`

- [ ] **Step 1: Add state to chat page**

In `page.tsx`, alongside existing `financialReport` state:

```typescript
import type { DataTableData } from "@/lib/chat-stream";

// Add state:
const [dataTable, setDataTable] = useState<DataTableData | null>(null);
const dataTablesRef = useRef<Map<string, DataTableData>>(new Map());
```

In `handleSend`, reset `dataTable`:
```typescript
setDataTable(null);
```

In stream handlers, add:
```typescript
onDataTable: (data) => setDataTable(data),
```

In `onMessage` handler, persist data table:
```typescript
onMessage: (message) => {
  // Associate in-flight financial report with this message
  setFinancialReport((current) => {
    if (current) financialReportsRef.current.set(message.id, current);
    return null;
  });
  // Associate in-flight data table with this message
  setDataTable((current) => {
    if (current) dataTablesRef.current.set(message.id, current);
    return null;
  });
  setStreamingMessage(message);
  // ...
},
```

In `finally` block, clear:
```typescript
setDataTable(null);
```

Pass to `MessageList`:
```typescript
<MessageList
  // ... existing props
  dataTable={dataTable}
  dataTables={dataTablesRef.current}
/>
```

- [ ] **Step 2: Update MessageList props and rendering**

In `message-list.tsx`, add to `MessageListProps`:
```typescript
dataTable?: DataTableData | null;
dataTables?: Map<string, DataTableData>;
```

Import:
```typescript
import { DataFrameTable } from "@/components/chat/data-frame-table";
import type { DataTableData } from "@/lib/chat-stream";
```

**Streaming container:** Add `<DataFrameTable />` rendering when `dataTable` is present (same location as `financialReport` rendering):
```typescript
{dataTable && <DataFrameTable data={dataTable} queryText={dataTable.query} />}
```

**Persisted messages:** In `AssistantMessageRow`, look up data table from map:
```typescript
const dataTableData = dataTables?.get(message.id);
// Render above text content:
{dataTableData && <DataFrameTable data={dataTableData} queryText={dataTableData.query} />}
```

Pass `dataTables` through from `MessageList` to `AssistantMessageRow` (same pattern as `financialReports`).

- [ ] **Step 3: Verify compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

Run: `cd frontend && npm run build`
Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/\(dashboard\)/chat/page.tsx frontend/src/components/chat/message-list.tsx
git commit -m "feat: wire DataFrameTable into chat streaming and persisted messages"
```

---

## Chunk 3: Save Query from DataFrame

### Task 5: Add "Save Query" button to DataFrameTable

**Context:** The existing saved query infrastructure is fully functional: `useCreateSavedQuery` hook → `POST /api/v1/skills` → `SavedSuiteQLQuery` model. We just need a save button in the `<DataFrameTable />` footer that collects a name and calls the mutation. Follow the exact same `SaveQueryBar` pattern from `suiteql-tool-card.tsx`.

**Files:**
- Modify: `frontend/src/components/chat/data-frame-table.tsx`

- [ ] **Step 1: Implement `SaveQueryButton` component**

Replace the stub `SaveQueryButton` in `data-frame-table.tsx`:

```typescript
import { Bookmark, Loader2, Pencil, X } from "lucide-react";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";

function SaveQueryButton({ queryText }: { queryText: string; onSave: (q: string) => void }) {
  const [mode, setMode] = useState<"idle" | "editing" | "saved">("idle");
  const [name, setName] = useState("");
  const mutation = useCreateSavedQuery();

  const handleSave = () => {
    if (!name.trim() || !queryText.trim()) return;
    mutation.mutate(
      { name: name.trim(), query_text: queryText.trim() },
      { onSuccess: () => setMode("saved") },
    );
  };

  if (mode === "saved") {
    return (
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
        <Check className="h-3 w-3" />
        Saved to Analytics
      </div>
    );
  }

  if (mode === "editing") {
    return (
      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Query name..."
            className="w-full rounded-md border bg-background px-2.5 py-1 pr-7 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSave();
              if (e.key === "Escape") setMode("idle");
            }}
          />
          <Pencil className="absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground/50" />
        </div>
        <button
          onClick={handleSave}
          disabled={!name.trim() || mutation.isPending}
          className="flex shrink-0 items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : "Save"}
        </button>
        <button
          onClick={() => setMode("idle")}
          className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="h-3 w-3" />
        </button>
        {mutation.isError && (
          <span className="truncate text-[11px] text-destructive">Failed to save</span>
        )}
      </div>
    );
  }

  return (
    <button
      onClick={() => setMode("editing")}
      className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:text-primary"
    >
      <Bookmark className="h-3 w-3" />
      Save to Analytics
    </button>
  );
}
```

Also update the footer in `DataFrameTable` to always show the save button when `queryText` is available (remove the `onSaveQuery` prop indirection):

```typescript
{/* Footer */}
<div className="flex items-center justify-between border-t px-4 py-2">
  <p className="text-[11px] text-muted-foreground">
    {truncated
      ? `Showing ${rows.length} of ${row_count} rows`
      : `${row_count} row${row_count === 1 ? "" : "s"} returned`}
  </p>
  {queryText && <SaveQueryButton queryText={queryText} onSave={() => {}} />}
</div>
```

- [ ] **Step 2: Remove the `onSaveQuery` prop (simplify)**

The `SaveQueryButton` now self-contains the mutation. Remove `onSaveQuery` from `DataFrameTableProps`:

```typescript
interface DataFrameTableProps {
  data: DataTableData;
  queryText?: string;
}
```

Update callers in `message-list.tsx` and `page.tsx` — just pass `queryText`, no callback needed.

- [ ] **Step 3: Verify compilation and build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: No errors

- [ ] **Step 4: Verify lint passes**

Run: `cd frontend && npm run lint`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/chat/data-frame-table.tsx frontend/src/components/chat/message-list.tsx
git commit -m "feat: add Save Query button to DataFrameTable component"
```

---

## Chunk 4: Integration Testing & Cleanup

### Task 6: Backend integration — rebuild and smoke test

**Context:** Rebuild Docker container, test with a live SuiteQL query, and verify the full flow: tool execution → SSE `data_table` event → `<DataFrameTable />` rendering → save query.

**Files:** None (manual verification)

- [ ] **Step 1: Run full backend test suite**

Run: `backend/.venv/bin/python -m pytest backend/tests/ -x -v 2>&1 | tail -20`
Expected: All tests PASS

- [ ] **Step 2: Run full frontend build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: No errors

- [ ] **Step 3: Rebuild Docker containers**

Run: `docker compose up -d --build backend && docker compose up -d --build --renew-anon-volumes frontend`
Expected: Both containers start successfully

- [ ] **Step 4: Smoke test checklist**

1. Open chat, start new session
2. Ask a SuiteQL question (e.g., "show me recent sales orders")
3. Verify: `<DataFrameTable />` renders with columns, rows, sorting, copy/CSV buttons
4. Verify: LLM provides commentary without rebuilding the table
5. Click "Save to Analytics" → enter name → verify save succeeds
6. Ask a financial report question (e.g., "income statement for Feb 2026")
7. Verify: `<FinancialReport />` still renders correctly (no regression)
8. Verify: both components work in same session

- [ ] **Step 5: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: integration cleanup for generic DataFrame feature"
```

---

## Implementation Notes

### Key Decisions

1. **Separate components**: `<FinancialReport />` stays for financial data (sections, accounting formatting, computed rows). `<DataFrameTable />` handles generic tabular data (flat table, sortable columns). No merging — they serve different UX needs.

2. **Reuse existing saved query API**: No new migration needed. The `/api/v1/skills` endpoints and `SavedSuiteQLQuery` model already handle CRUD. The `SaveQueryButton` in `<DataFrameTable />` calls `useCreateSavedQuery()` directly.

3. **List-of-lists preserved**: SuiteQL returns `columns: string[]` + `rows: unknown[][]`. The `<DataFrameTable />` works with this format directly — no conversion to list-of-dicts needed.

4. **Condensed result for LLM**: SuiteQL condensed result includes `columns` (so LLM knows what data was returned) but omits `rows` (saves tokens). LLM can still discuss the data intelligently.

5. **Both paths covered**: The `tool_result_interceptor` callback works in both the unified agent path (Framework tenant) and the raw agentic loop (all other tenants).

### Risk Areas

- **Token savings vs. LLM accuracy**: If the LLM needs to reference specific row values for analysis, the condensed result won't have them. The note instructs commentary-only, which works for summaries but may frustrate users wanting the LLM to compute aggregates from the data. Monitor and adjust if needed.
- **Large result sets**: SuiteQL can return up to 1000 rows. The SSE event sends all rows to the frontend. For very large tables, this is fine (JSON serialization handles it), but rendering 1000 rows in the DOM may lag. The `max-h-[600px]` overflow scroll mitigates this — no virtualization needed initially.
