# Excel Export from Saved Queries — Plan

## Problem

Current export is CSV-only via Celery task (`suiteql_export.py`). The output is a flat, unformatted dump — no headers styling, no number formatting, no column widths, no branding. Users get a generic CSV they have to manually format before sharing with stakeholders.

The chat's `SuiteQLToolCard` has "Save to Analytics" but no "Export to Excel" button. The `QueryPreviewModal` in Analytics has an export button that triggers the CSV Celery task, but the download endpoint (`/api/v1/skills/exports/{fileName}`) isn't even implemented.

**What users want**: Click "Export" on any query result in chat or Analytics → get a beautifully formatted `.xlsx` file they can send directly to their CFO.

## Solution

Add an Excel export pipeline that takes any `columns + rows` result (from SuiteQL, saved searches, MCP reports) and produces a professionally formatted `.xlsx` using openpyxl. Two trigger points: (1) button on `SuiteQLToolCard` in chat, (2) button on `QueryPreviewModal` in Analytics.

## Architecture

```
User clicks "Export Excel" on query result card
    ↓
Frontend POST /api/v1/exports/excel  { columns, rows, metadata }
    ↓
Backend: excel_export_service.generate() → openpyxl → styled .xlsx
    ↓
Returns StreamingResponse with Content-Disposition: attachment
    ↓
Browser downloads the file
```

No Celery needed — openpyxl is fast enough for <10K rows synchronously. For massive exports (>10K rows), optionally queue via Celery and poll like the existing CSV flow.

## Backend Changes

### 1. New service: `backend/app/services/excel_export_service.py`

The core formatter. Takes generic `columns + rows` and produces a styled workbook.

```python
"""Professional Excel export with financial-grade formatting."""

import io
from datetime import datetime, timezone
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    NamedStyle,
    PatternFill,
    Side,
    numbers,
)
from openpyxl.utils import get_column_letter


class ExcelExportConfig:
    """Branding & style configuration per tenant."""
    brand_name: str = "SuiteStudio"
    brand_color_hex: str = "1a73e8"       # Primary brand color
    header_bg_hex: str = "1a73e8"          # Column header background
    header_font_color: str = "FFFFFF"      # Column header text
    subheader_bg_hex: str = "e8f0fe"       # Section subtotal background
    stripe_bg_hex: str = "f8f9fa"          # Alternating row background
    font_name: str = "Arial"
    font_size: int = 10
    header_font_size: int = 10
    title_font_size: int = 14


def generate_excel(
    columns: list[str],
    rows: list[list[Any]],
    *,
    title: str = "Query Results",
    sheet_name: str = "Data",
    config: ExcelExportConfig | None = None,
    metadata: dict[str, str] | None = None,
    column_types: dict[str, str] | None = None,  # e.g. {"amount": "currency", "rate": "percent"}
) -> io.BytesIO:
    """Generate a professionally formatted Excel workbook.

    Args:
        columns: Column headers
        rows: Data rows (list of lists, aligned to columns)
        title: Report title shown in the title row
        sheet_name: Excel sheet tab name
        config: Branding/style config (defaults to SuiteStudio theme)
        metadata: Key-value pairs for the info block (e.g. {"Period": "Jan 2026", "Account": "Framework"})
        column_types: Override auto-detected column types

    Returns:
        BytesIO buffer containing the .xlsx file
    """
    cfg = config or ExcelExportConfig()
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.sheet_properties.tabColor = cfg.brand_color_hex

    # --- Styles ---
    thin_border = Border(
        bottom=Side(style="thin", color="E0E0E0"),
    )
    header_fill = PatternFill("solid", fgColor=cfg.header_bg_hex)
    header_font = Font(
        name=cfg.font_name, size=cfg.header_font_size,
        bold=True, color=cfg.header_font_color,
    )
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    stripe_fill = PatternFill("solid", fgColor=cfg.stripe_bg_hex)
    body_font = Font(name=cfg.font_name, size=cfg.font_size)
    body_alignment = Alignment(vertical="center")
    number_alignment = Alignment(horizontal="right", vertical="center")

    title_font = Font(
        name=cfg.font_name, size=cfg.title_font_size,
        bold=True, color=cfg.brand_color_hex,
    )
    meta_font = Font(name=cfg.font_name, size=9, color="666666")

    # --- Title block ---
    current_row = 1
    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=min(len(columns), 6))
    title_cell = ws.cell(row=current_row, column=1, value=title)
    title_cell.font = title_font
    title_cell.alignment = Alignment(vertical="center")
    current_row += 1

    # --- Metadata block ---
    if metadata:
        for key, value in metadata.items():
            ws.cell(row=current_row, column=1, value=f"{key}:").font = Font(
                name=cfg.font_name, size=9, bold=True, color="333333",
            )
            ws.cell(row=current_row, column=2, value=value).font = meta_font
            current_row += 1

    # Timestamp
    ws.cell(row=current_row, column=1, value="Generated:").font = Font(
        name=cfg.font_name, size=9, bold=True, color="333333",
    )
    ws.cell(
        row=current_row, column=2,
        value=datetime.now(timezone.utc).strftime("%b %d, %Y %I:%M %p UTC"),
    ).font = meta_font
    current_row += 1

    # Spacer row
    current_row += 1

    # --- Column headers ---
    header_row = current_row
    for col_idx, col_name in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=_humanize_header(col_name))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = Border(
            bottom=Side(style="medium", color=cfg.brand_color_hex),
        )
    current_row += 1

    # --- Auto-detect column types ---
    detected_types = _detect_column_types(columns, rows)
    if column_types:
        detected_types.update(column_types)

    # --- Data rows ---
    for row_idx, row_data in enumerate(rows):
        excel_row = current_row + row_idx
        is_striped = row_idx % 2 == 1

        for col_idx, value in enumerate(row_data):
            cell = ws.cell(row=excel_row, column=col_idx + 1)
            col_type = detected_types.get(columns[col_idx], "text")

            # Write value with type-appropriate formatting
            _write_typed_cell(cell, value, col_type)

            # Style
            cell.font = body_font
            cell.border = thin_border
            if col_type in ("currency", "number", "percent"):
                cell.alignment = number_alignment
            else:
                cell.alignment = body_alignment

            if is_striped:
                cell.fill = stripe_fill

    # --- Auto-fit column widths ---
    for col_idx, col_name in enumerate(columns, start=1):
        col_letter = get_column_letter(col_idx)
        # Sample header + first 50 data rows for width
        max_len = len(_humanize_header(col_name))
        for row_data in rows[:50]:
            if col_idx - 1 < len(row_data):
                cell_len = len(str(row_data[col_idx - 1] or ""))
                max_len = max(max_len, cell_len)
        # Clamp between 8 and 45 characters
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 8), 45)

    # --- Freeze panes (header row) ---
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    # --- Print setup ---
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = f"{header_row}:{header_row}"

    # --- Footer ---
    footer_row = current_row + len(rows) + 1
    ws.cell(row=footer_row, column=1, value=f"{len(rows)} rows").font = Font(
        name=cfg.font_name, size=9, italic=True, color="999999",
    )

    # Write to buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def _humanize_header(col: str) -> str:
    """Convert snake_case or camelCase to Title Case."""
    import re
    # camelCase → spaced
    col = re.sub(r"([a-z])([A-Z])", r"\1 \2", col)
    # snake_case → spaced
    col = col.replace("_", " ")
    return col.strip().title()


def _detect_column_types(
    columns: list[str], rows: list[list[Any]]
) -> dict[str, str]:
    """Auto-detect column types by sampling data.

    Returns dict of column_name → type ("currency", "number", "percent", "date", "text")
    """
    # Heuristic: column name patterns
    CURRENCY_PATTERNS = {"amount", "balance", "total", "price", "cost", "revenue", "income", "expense", "debit", "credit", "net", "gross", "payment", "refund"}
    PERCENT_PATTERNS = {"rate", "margin", "percent", "pct", "ratio"}
    DATE_PATTERNS = {"date", "created", "modified", "updated", "posted", "period"}
    ID_PATTERNS = {"id", "internalid", "tranid", "acctnumber", "number", "num", "code"}

    types: dict[str, str] = {}

    for col_idx, col_name in enumerate(columns):
        lower = col_name.lower().replace("_", "").replace(" ", "")

        # Name-based heuristics first
        if any(p in lower for p in ID_PATTERNS):
            types[col_name] = "text"  # IDs should never be formatted as numbers
            continue
        if any(p in lower for p in CURRENCY_PATTERNS):
            types[col_name] = "currency"
            continue
        if any(p in lower for p in PERCENT_PATTERNS):
            types[col_name] = "percent"
            continue
        if any(p in lower for p in DATE_PATTERNS):
            types[col_name] = "date"
            continue

        # Data-based detection: sample first 20 non-null values
        sample_values = []
        for row in rows[:20]:
            if col_idx < len(row) and row[col_idx] is not None:
                sample_values.append(row[col_idx])

        if not sample_values:
            types[col_name] = "text"
            continue

        # Check if numeric
        numeric_count = 0
        for v in sample_values:
            if isinstance(v, (int, float)):
                numeric_count += 1
            elif isinstance(v, str):
                try:
                    float(v.replace(",", "").replace("$", "").replace("(", "-").replace(")", ""))
                    numeric_count += 1
                except ValueError:
                    pass

        if numeric_count / len(sample_values) >= 0.7:
            types[col_name] = "number"
        else:
            types[col_name] = "text"

    return types


def _write_typed_cell(cell, value: Any, col_type: str) -> None:
    """Write a value to a cell with appropriate type and number format."""
    if value is None:
        cell.value = ""
        return

    if col_type == "currency":
        num = _to_number(value)
        if num is not None:
            cell.value = num
            cell.number_format = '#,##0.00;(#,##0.00);"-"'
        else:
            cell.value = str(value)

    elif col_type == "number":
        num = _to_number(value)
        if num is not None:
            cell.value = num
            if isinstance(num, float) and not num.is_integer():
                cell.number_format = "#,##0.00"
            else:
                cell.number_format = "#,##0"
        else:
            cell.value = str(value)

    elif col_type == "percent":
        num = _to_number(value)
        if num is not None:
            # If value > 1, assume it's already a percentage (e.g. 45.2 = 45.2%)
            if abs(num) > 1:
                cell.value = num / 100
            else:
                cell.value = num
            cell.number_format = "0.0%"
        else:
            cell.value = str(value)

    elif col_type == "date":
        cell.value = str(value)  # Keep as-is; NetSuite dates are already formatted

    else:
        cell.value = str(value) if value is not None else ""


def _to_number(value: Any) -> float | int | None:
    """Coerce a value to a number, handling common formats."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        # Handle accounting parens: (123.45) → -123.45
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            num = float(cleaned)
            return int(num) if num.is_integer() else num
        except ValueError:
            return None
    return None
```

### 2. New endpoint: `backend/app/api/v1/exports.py`

```python
"""Excel and CSV export endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.services import audit_service, excel_export_service

router = APIRouter(prefix="/exports", tags=["exports"])


class ExcelExportRequest(BaseModel):
    """Request body for Excel export."""
    columns: list[str] = Field(min_length=1)
    rows: list[list[Any]]
    title: str = "Query Results"
    metadata: dict[str, str] | None = None
    column_types: dict[str, str] | None = None


@router.post("/excel")
async def export_excel(
    request: ExcelExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Generate and download a formatted Excel file from query results."""
    if len(request.rows) > 50_000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Excel export limited to 50,000 rows. Use CSV for larger datasets.",
        )

    # Build tenant-branded config
    config = excel_export_service.ExcelExportConfig()
    # TODO: Pull brand_color_hex from tenant_configs if white-label is active

    buffer = excel_export_service.generate_excel(
        columns=request.columns,
        rows=request.rows,
        title=request.title,
        config=config,
        metadata=request.metadata,
        column_types=request.column_types,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="export",
        action="export.excel",
        actor_id=user.id,
        resource_type="query_result",
        details={"row_count": len(request.rows), "column_count": len(request.columns)},
    )
    await db.commit()

    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in request.title)[:50]
    filename = f"{safe_title}.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

Register in `backend/app/api/v1/router.py`:
```python
from app.api.v1.exports import router as exports_router
api_router.include_router(exports_router)
```

### 3. Fix the missing CSV download endpoint

The `QueryPreviewModal` references `/api/v1/skills/exports/{fileName}` but it doesn't exist. Add it:

```python
# In backend/app/api/v1/skills.py or exports.py
@router.get("/exports/{file_name}")
async def download_export(
    file_name: str,
    user: Annotated[User, Depends(get_current_user)],
):
    """Download a previously generated export file."""
    import os
    from fastapi.responses import FileResponse

    export_dir = os.environ.get("EXPORT_DIR", "/tmp/exports")
    file_path = os.path.join(export_dir, file_name)

    # Security: prevent path traversal
    if ".." in file_name or "/" in file_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Export file not found")

    return FileResponse(
        file_path,
        media_type="text/csv",
        filename=file_name,
    )
```

## Frontend Changes

### 1. Export hook: `frontend/src/hooks/use-excel-export.ts`

```typescript
"use client";

import { useState } from "react";
import { apiClient } from "@/lib/api-client";

interface ExcelExportParams {
  columns: string[];
  rows: unknown[][];
  title?: string;
  metadata?: Record<string, string>;
  columnTypes?: Record<string, string>;
}

export function useExcelExport() {
  const [isExporting, setIsExporting] = useState(false);

  const exportToExcel = async (params: ExcelExportParams) => {
    setIsExporting(true);
    try {
      const response = await fetch("/api/v1/exports/excel", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiClient.getToken()}`,
        },
        body: JSON.stringify({
          columns: params.columns,
          rows: params.rows,
          title: params.title ?? "Query Results",
          metadata: params.metadata,
          column_types: params.columnTypes,
        }),
      });

      if (!response.ok) {
        throw new Error(`Export failed: ${response.statusText}`);
      }

      // Trigger browser download
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = params.title
        ? `${params.title.replace(/[^a-zA-Z0-9 _-]/g, "_")}.xlsx`
        : "export.xlsx";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } finally {
      setIsExporting(false);
    }
  };

  return { exportToExcel, isExporting };
}
```

### 2. Add "Export Excel" button to `SuiteQLToolCard`

In `frontend/src/components/chat/suiteql-tool-card.tsx`, add next to "Save to Analytics":

```tsx
import { Download, FileSpreadsheet } from "lucide-react";
import { useExcelExport } from "@/hooks/use-excel-export";

// Inside SuiteQLToolCard component:
const { exportToExcel, isExporting } = useExcelExport();

const handleExport = () => {
  if (!resultPayload) return;
  exportToExcel({
    columns: resultPayload.columns,
    rows: resultPayload.rows,
    title: userQuestion?.slice(0, 80) ?? toolLabel,
    metadata: {
      "Source": toolLabel,
      "Rows": String(resultPayload.row_count),
      ...(resultPayload.truncated ? { "Note": "Results were truncated" } : {}),
    },
  });
};

// In the footer area, next to SaveQueryBar:
<div className="flex items-center gap-3">
  <button
    onClick={handleExport}
    disabled={isExporting}
    className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground hover:text-primary transition-colors disabled:opacity-50"
  >
    {isExporting ? (
      <Loader2 className="h-3 w-3 animate-spin" />
    ) : (
      <FileSpreadsheet className="h-3 w-3" />
    )}
    Export Excel
  </button>
  <SaveQueryBar ... />
</div>
```

### 3. Add "Export Excel" to `QueryPreviewModal`

Same pattern — add the `useExcelExport` hook and a button alongside the existing CSV export.

## Formatting Spec — What Makes It "Beautiful"

### Title block (rows 1-5)
- Row 1: Report title in brand color, 14pt bold, merged across 6 columns
- Rows 2-4: Metadata (Source, Period, Account, etc.) in 9pt gray
- Row 5: "Generated: Mar 12, 2026 3:45 PM UTC" timestamp

### Column headers (row 6)
- Background: Brand primary color (deep blue `#1a73e8`)
- Text: White, 10pt bold, centered, wrap text enabled
- Bottom border: Medium weight in brand color
- Freeze panes below this row

### Data rows
- Font: Arial 10pt, consistent across all cells
- Alternating row stripes: Light gray `#f8f9fa` on even rows
- Subtle bottom border on every row: `#E0E0E0`
- Right-aligned for all numeric columns
- Left-aligned for text columns

### Number formatting
- Currency: `#,##0.00` with accounting-style parentheses for negatives: `(1,234.56)`
- Integers: `#,##0` with comma grouping
- Percentages: `0.0%`
- IDs/codes: Always text (never formatted as numbers — no `4,000` for account `4000`)
- Nulls: Empty cell (not "null" or "None")

### Column widths
- Auto-fit based on header + first 50 rows of data
- Min width: 8 characters
- Max width: 45 characters
- Prevents the classic "######" overflow

### Print setup
- Landscape orientation
- Fit to page width
- Header row repeats on every printed page

### Footer
- Row count in 9pt italic gray below the data

### Tab
- Sheet tab colored in brand primary

## Smart Type Detection

The service auto-detects column types using two strategies:

**Name-based heuristics** (checked first):
- `amount`, `balance`, `total`, `price`, `cost`, `revenue`, `income`, `expense`, `debit`, `credit` → currency
- `rate`, `margin`, `percent`, `pct`, `ratio` → percent
- `date`, `created`, `modified`, `posted`, `period` → date
- `id`, `internalid`, `tranid`, `acctnumber`, `number`, `code` → text (never numeric)

**Data sampling** (fallback):
- Sample first 20 non-null values per column
- If ≥70% parse as numbers → numeric
- Otherwise → text

**Manual override**: The frontend can pass `columnTypes` for specific columns when the heuristic doesn't match (e.g., a column named "ref" that contains dollar amounts).

## Data Flow Summary

```
1. SuiteQL / Saved Search / MCP Report executes
     ↓
2. tool_call_results.py normalizes to { columns, rows } (already exists)
     ↓
3. Frontend renders in SuiteQLToolCard with [Export Excel] button
     ↓
4. User clicks → POST /api/v1/exports/excel { columns, rows, title, metadata }
     ↓
5. excel_export_service.generate_excel() → openpyxl → styled .xlsx in BytesIO
     ↓
6. StreamingResponse → browser download
```

## Tenant Branding (Phase 2)

Pull brand colors from `tenant_configs`:

```python
# In the export endpoint, before calling generate_excel():
from app.services.settings_service import get_tenant_config

tenant_config = await get_tenant_config(db, user.tenant_id)
if tenant_config and tenant_config.brand_color_hsl:
    config.brand_color_hex = hsl_to_hex(tenant_config.brand_color_hsl)
    config.header_bg_hex = config.brand_color_hex
```

This means each tenant's exports are on-brand — Framework's exports look different from Rails' exports.

## Report-Specific Templates (Phase 3)

For financial reports specifically, add section awareness:

```python
def generate_financial_excel(
    columns: list[str],
    rows: list[list[Any]],
    sections: list[dict],  # [{"key": "Revenue", "subtotal_row": {...}}, ...]
    grand_totals: dict,     # {"gross_profit": 2729242, "net_income": 57941}
) -> io.BytesIO:
    """Financial report with section grouping, subtotals, and grand total rows."""
    # Same base styling as generate_excel, plus:
    # - Section header rows: bold, slightly darker background
    # - Subtotal rows: bold, border-top medium
    # - Grand total rows (Gross Profit, Net Income): bold, double-border-top, brand-colored text
    # - Indent detail rows under sections
    ...
```

This connects with the `<FinancialReport />` component plan — the same structured data that feeds the chat component also feeds the Excel export.

## Implementation Order

### Phase 0 — Pagination fix (prerequisite)
1. **Wire `paginate=True`** into `netsuite_suiteql.py` OAuth 2.0 path (1-line change)
2. **Fix Celery CSV export** — delegate to `execute_suiteql_via_rest(paginate=True)` instead of own loop
3. **Test** pagination with 2,500+ row queries

### Phase 1 — Excel export (core)
5. **`excel_export_service.py`** — Core formatter (can be tested standalone)
6. **`/api/v1/exports/excel` endpoint** — Direct data export with auth + audit
7. **`/api/v1/exports/query-export` endpoint** — Re-execute query with full pagination + export
8. **`use-excel-export.ts` hook** — Frontend download logic (smart: direct vs re-execute)
9. **`SuiteQLToolCard` export button** — Primary trigger point
10. **`QueryPreviewModal` export button** — Secondary trigger point
11. **Fix missing CSV download endpoint** — Unblock existing CSV flow

### Phase 2 — Branding
12. **Tenant branding** — Pull colors from tenant_configs

### Phase 3 — Financial templates
13. **Financial report template** — Section-aware formatting with subtotals/grand totals

## Testing

### Unit tests (`backend/tests/test_excel_export_service.py`)
- [ ] Basic export: columns + rows → valid .xlsx
- [ ] Currency detection: "amount" column formatted with `#,##0.00`
- [ ] ID detection: "internalid" column stays text (no comma grouping)
- [ ] Negative numbers: Parentheses format `(1,234.56)`
- [ ] Null handling: None → empty cell, not "None"
- [ ] Empty rows: No crash on []
- [ ] Large dataset: 10K rows completes in <2s
- [ ] Column width clamping: Min 8, max 45
- [ ] Metadata block: Title, metadata dict, timestamp all present

### Integration tests
- [ ] POST /api/v1/exports/excel returns valid xlsx content-type
- [ ] Auth required (401 without token)
- [ ] Audit log created on export
- [ ] 50K row limit enforced (400 error)

### Manual QA
- [ ] Open exported file in Excel — no "######" columns
- [ ] Print preview — headers repeat, landscape fits
- [ ] Negatives show in parens, not minus signs
- [ ] Account numbers (4000, 4010) are NOT comma-formatted (not "4,000")
- [ ] Brand color visible in header row

## Dependencies

- `openpyxl` — already in requirements (used by xlsx skill)
- No new pip packages needed

## Prerequisite: SuiteQL Pagination Fix

### The Problem

NetSuite REST API returns max **1000 rows per page** but supports pagination via `offset` + `hasMore`. We have three execution paths, and only one paginates correctly:

| Path | File | Paginates? | Issue |
|------|------|-----------|-------|
| `execute_suiteql_via_rest()` | `netsuite_client.py` | ✅ When `paginate=True` | Already built, but never called with `paginate=True` |
| `execute()` (chat tool, OAuth 2.0) | `netsuite_suiteql.py` | ❌ | Calls `execute_suiteql()` without `paginate=True` |
| `export_suiteql_to_csv()` | `suiteql_export.py` | ⚠️ Buggy | Appends `OFFSET + FETCH FIRST` to user's query — conflicts with existing FETCH FIRST clauses from `enforce_limit()` |

> **Note**: Our app only uses OAuth 2.0. The OAuth 1.0 path in `netsuite_suiteql.py` is legacy/unused and can be ignored for this fix.

**Result**: Chat queries are silently capped at 1000 rows even when the user asks for 5000. CSV exports may double-up FETCH FIRST clauses. Excel exports need full data but have no paginated source.

### The Fix

#### 1. Wire pagination into `netsuite_client.execute_suiteql()`

The `execute_suiteql_via_rest()` already supports `paginate=True`. Just need to pass it through:

```python
# netsuite_client.py — execute_suiteql() already accepts paginate kwarg
# No changes needed here — it's already wired

# netsuite_suiteql.py — execute() function, OAuth 2.0 path (line ~383)
# Change from:
result = await execute_suiteql(
    access_token, account_id, query, max_rows,
    timeout_seconds=timeout_seconds,
)
# Change to:
result = await execute_suiteql(
    access_token, account_id, query, max_rows,
    paginate=True,  # <-- Enable pagination for all chat queries
    timeout_seconds=timeout_seconds,
)
```

#### 2. Fix Celery CSV export pagination

The current `suiteql_export.py` appends `OFFSET + FETCH FIRST` to the raw query, which can conflict with existing FETCH FIRST from `enforce_limit()`. Fix: strip existing FETCH FIRST before adding pagination, and use the REST API's native `?limit=&offset=` params instead:

```python
# suiteql_export.py — replace the pagination loop:

# Strip any existing FETCH FIRST / LIMIT from the query
import re
base_query = query_text.rstrip().rstrip(";").rstrip()
base_query = re.sub(r"\s+FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY", "", base_query, flags=re.IGNORECASE)
base_query = re.sub(r"\s+LIMIT\s+\d+", "", base_query, flags=re.IGNORECASE)

# Use REST API offset pagination (don't modify the SQL)
while True:
    result = loop.run_until_complete(
        execute_suiteql_via_rest(
            access_token, account_id, base_query, limit=100_000,
            paginate=True, timeout_seconds=60,
        )
    )
    # execute_suiteql_via_rest with paginate=True already handles the loop
    # So we can just use it directly instead of our own loop
    all_rows = result.get("rows", [])
    columns = result.get("columns", [])
    break  # paginate=True already fetched everything
```

Actually, simpler: just delegate entirely to `execute_suiteql_via_rest(paginate=True)` which already handles the full loop. The Celery task's own loop is redundant.

#### 3. New: Full-data export endpoint

For Excel/CSV exports that need ALL rows (not just what's in the chat card's `resultPayload`), add a server-side re-execution path:

```python
# In exports.py — add an alternative endpoint that re-runs the query with full pagination

class QueryExportRequest(BaseModel):
    """Export by re-running a saved query with full pagination."""
    query_text: str
    title: str = "Query Results"
    format: Literal["xlsx", "csv"] = "xlsx"
    metadata: dict[str, str] | None = None
    column_types: dict[str, str] | None = None

@router.post("/query-export")
async def export_from_query(
    request: QueryExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Re-execute a SuiteQL query with full pagination and export to Excel/CSV."""
    # Get NetSuite connection
    connection = await _get_netsuite_connection(db, user.tenant_id)
    access_token = await get_valid_token(db, connection)

    # Validate query is read-only
    validate_query(request.query_text, allowed_tables)

    # Execute with full pagination — no row cap for exports
    result = await execute_suiteql_via_rest(
        access_token, account_id, request.query_text,
        limit=100_000,  # Safety cap at 100K rows
        paginate=True,
        timeout_seconds=120,
    )

    columns = result["columns"]
    rows = result["rows"]

    if request.format == "xlsx":
        buffer = excel_export_service.generate_excel(
            columns=columns, rows=rows,
            title=request.title,
            metadata={
                **(request.metadata or {}),
                "Total Rows": str(len(rows)),
            },
            column_types=request.column_types,
        )
        return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ...)
    else:
        # CSV path
        buffer = _generate_csv(columns, rows)
        return StreamingResponse(buffer, media_type="text/csv", ...)
```

This means:
- **Small results** (<1000 rows already in the card): Frontend sends `columns + rows` directly → instant Excel
- **Large results** (truncated, need full data): Frontend sends the `query_text` → backend re-executes with pagination → Excel/CSV

### Frontend: Smart Export Button

The `SuiteQLToolCard` export button checks whether data is truncated:

```tsx
const handleExport = () => {
  if (!resultPayload) return;

  if (resultPayload.truncated) {
    // Data was truncated — need server-side re-execution with full pagination
    exportFromQuery({
      query_text: queryText,
      title: userQuestion?.slice(0, 80) ?? toolLabel,
      format: "xlsx",
      metadata: { Source: toolLabel },
    });
  } else {
    // All data is already in the card — direct export
    exportToExcel({
      columns: resultPayload.columns,
      rows: resultPayload.rows,
      title: userQuestion?.slice(0, 80) ?? toolLabel,
      metadata: { Source: toolLabel, Rows: String(resultPayload.row_count) },
    });
  }
};
```

### Testing

- [ ] Query returning 50 rows → single page, no pagination needed → works as before
- [ ] Query returning 2,500 rows → 3 pages (1000 + 1000 + 500) → all rows returned
- [ ] Query returning 10,000 rows → 10 pages → all rows in export
- [ ] Query with existing `FETCH FIRST 500 ROWS ONLY` → respects the cap, returns ≤500
- [ ] CSV export of 5,000 rows → no duplicate FETCH FIRST clauses
- [ ] Export button on truncated result → re-executes query → full data in Excel

## Open Questions

- Should we add a "Copy as Excel-pasteable" button (tab-separated clipboard) for quick paste into Google Sheets?
- Should the financial template (Phase 3) support balance sheet T-account layout (assets left, liabilities right)?
- Should exports be stored/cached for re-download, or always generate fresh?
- For very large exports (>50K rows), should we use Celery async + polling instead of synchronous?
