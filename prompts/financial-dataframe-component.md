# Financial DataFrame Component — Plan

## Problem

The LLM constructs markdown tables from raw row data, causing:
- Column misalignment (subtotal rows shift left)
- Inconsistent number formatting (sometimes $, sometimes not, inconsistent parens)
- No interactivity (can't sort, filter, collapse sections)
- Every response re-renders the table differently
- Wastes tokens on table formatting instead of analysis

## Solution

Return structured JSON from the financial report tool. Frontend renders it in a dedicated `<FinancialReport />` component. The LLM provides commentary only — never builds tables.

## Architecture

```
Tool returns structured data
    ↓
SSE streams a special event: { type: "financial_report", data: { ... } }
    ↓
Frontend detects event, renders <FinancialReport /> component inline in chat
    ↓
LLM adds text commentary above/below (no table formatting)
```

## Backend Changes

### 1. New SSE event type

In `orchestrator.py`, when the financial report tool returns successfully, emit a dedicated SSE event before the LLM processes the result:

```python
# After tool execution, before feeding result back to LLM
if tool_name == "netsuite.financial_report" and result.get("success"):
    yield {
        "type": "financial_report",
        "data": {
            "report_type": result["report_type"],
            "period": result["period"],
            "columns": result["columns"],
            "rows": result["items"],
            "summary": result["summary"],
        }
    }
```

### 2. Simplify what the LLM sees

Instead of feeding the LLM 500+ rows, return a condensed version:

```python
# What the LLM gets back as tool result (for commentary)
llm_result = {
    "success": True,
    "report_type": "income_statement_trend",
    "period": "Nov 2025, Dec 2025, Jan 2026, Feb 2026",
    "total_rows": 547,
    "summary": result["summary"],  # Just the pre-computed totals
    "note": "Full data rendered in the financial report component. Provide analysis and commentary only — do NOT rebuild the table."
}
```

This massively reduces token usage — the LLM gets summary numbers for commentary instead of 500 rows it has to format.

### 3. Update financial mode prompt

```
The financial report tool renders data directly in a visual table component.
Do NOT format tables yourself. Instead, provide:
1. A brief summary of the key findings
2. Notable trends or anomalies
3. Comparisons if the user asked for them
Reference the pre-computed summary numbers for your analysis.
```

## Frontend Changes

### 1. `<FinancialReport />` component

`frontend/src/components/chat/financial-report.tsx`

Features:
- **Column headers**: Acct#, Name, then one column per period (or single Amount column)
- **Section grouping**: Collapsible sections (Revenue, COGS, Operating Expense, etc.)
- **Subtotal rows**: Bold, with values from `summary` / `summary.by_period`
- **Number formatting**: `Intl.NumberFormat` — always `$X,XXX.XX`, negatives in `($X,XXX.XX)`
- **Grand totals**: Gross Profit, Operating Income, Net Income — highlighted rows
- **Sticky headers**: Column headers stick on scroll for large reports

```tsx
interface FinancialReportProps {
  reportType: string;
  period: string;
  columns: string[];
  rows: Record<string, any>[];
  summary: Record<string, any>;
}

// Section order for income statement
const INCOME_SECTIONS = [
  { key: "1-Revenue", label: "Revenue", subtotalKey: "total_revenue" },
  { key: "2-Other Income", label: "Other Income", subtotalKey: "total_other_income" },
  { key: "3-COGS", label: "Cost of Goods Sold", subtotalKey: "total_cogs" },
  // Computed: Gross Profit
  { key: "4-Operating Expense", label: "Operating Expenses", subtotalKey: "total_operating_expense" },
  // Computed: Operating Income
  { key: "5-Other Expense", label: "Other Expenses", subtotalKey: "total_other_expense" },
  // Computed: Net Income
];
```

### 2. Render in chat message stream

`frontend/src/components/chat/chat-message.tsx`

Detect the `financial_report` SSE event and render inline:

```tsx
// In the message stream handler
case "financial_report":
  return <FinancialReport {...event.data} />;
```

### 3. Export buttons

Built into the component header:
- **CSV** — `report.export` tool already exists, but client-side is faster
- **Copy table** — clipboard as tab-separated for Excel paste
- **PDF** — stretch goal, low priority

### 4. Dark mode styling

Match the existing chat UI dark theme. Use:
- `bg-card` for the table container
- `border-border` for grid lines
- `text-foreground` / `text-muted-foreground` for values
- Section headers: slightly darker `bg-muted` background
- Subtotal rows: `font-semibold`
- Grand total rows (Gross Profit, Net Income): `font-bold bg-muted/50`
- Negative values: `text-red-400` (dark) / `text-red-600` (light)

## Data Flow

### Single-period income statement

```
Tool returns:
  rows: [{ acctnumber, acctname, accttype, section, amount }]
  summary: { total_revenue, total_cogs, gross_profit, ... net_income }

Component renders:
  | Acct# | Account Name | Amount |
  | REVENUE |
  | 4000  | Sales        | $13,800,780 |
  | 4006  | Sales - SO   | ($406,501)  |
  | ...   | ...          | ...         |
  | **Total Revenue** |  | **$12,441,023** |
  | COGS |
  | ...  |
  | **Gross Profit** |  | **$2,729,242** |
  | ...  |
  | **Net Income** |     | **$57,941** |
```

### Trend (multi-period)

```
Tool returns:
  rows: [{ periodname, acctnumber, acctname, section, amount }]
  summary: { by_period: { "Nov 2025": {...}, "Dec 2025": {...}, ... } }

Component renders:
  | Acct# | Account Name | Nov 2025 | Dec 2025 | Jan 2026 | Feb 2026 |
  | REVENUE |
  | 4010  | Gross Sales  | $13,164,488 | $9,034,010 | $11,225,640 | $12,423,988 |
  | ...   |
  | **Total Revenue** | | **$10,304,258** | **$5,288,116** | **$8,813,572** | **$10,628,261** |
  | ...   |
  | **Net Income** | | **$1,120,888** | **($6,067,209)** | **$1,265,213** | **$1,620,183** |
```

## Token Savings

Current: LLM receives 500 rows → formats table → ~3,000-5,000 output tokens on table alone
After: LLM receives summary only → writes 2-3 sentences of commentary → ~200-400 output tokens

Estimated **80-90% reduction** in output tokens for financial reports.

## Implementation Order

1. `<FinancialReport />` component (render from static props)
2. SSE event emission from orchestrator
3. Chat message stream handler integration
4. Update financial mode prompt (commentary only)
5. Reduce tool result sent to LLM (summary only)
6. Export buttons (CSV, clipboard)
7. Section collapsing (nice-to-have)

## Open Questions

- Should balance sheet use a different layout? (Assets on left, Liabilities+Equity on right — T-account style)
- Should we cache the rendered report in the chat message for history replay?
- Do we want sparklines or mini-charts for trend data? (stretch goal)
