# Excel Export from Queries — Design Spec

## Problem

Current export is CSV-only via Celery task. Output is flat, unformatted. Users want a professionally formatted `.xlsx` they can send directly to stakeholders. Additionally, the CSV download endpoint referenced by `QueryPreviewModal` (`GET /api/v1/skills/exports/{fileName}`) is not implemented, so existing CSV exports can't be downloaded. SuiteQL pagination is also not wired through the chat tool's OAuth 2.0 path, silently capping results at 1000 rows.

## Scope

- **Phase 0**: Wire pagination into the chat SuiteQL tool and fix Celery CSV export
- **Phase 1**: Core Excel export service, API endpoints, frontend buttons
- **CSV download fix**: Implement the missing file download endpoint

Out of scope: tenant branding (Phase 2), financial report templates (Phase 3).

## Approach

Synchronous in-memory Excel generation via openpyxl. Returns `StreamingResponse` directly — no Celery, no polling. 50K row safety cap. Two export paths: direct (data in frontend) and re-execute (truncated data needs server-side re-query with full pagination).

## Backend

### New dependency

Add `openpyxl` to `backend/pyproject.toml` dependencies.

### New service: `backend/app/services/excel_export_service.py`

Core formatter. Takes generic `columns + rows`, returns a styled `io.BytesIO` buffer containing a `.xlsx` workbook.

**`ExcelExportConfig` dataclass:**
- `brand_name`, `brand_color_hex`, `header_bg_hex`, `header_font_color`
- `stripe_bg_hex`, `font_name`, `font_size`, `header_font_size`, `title_font_size`
- Defaults to SuiteStudio theme. Extensible for tenant branding in Phase 2.

**`generate_excel()` function:**
- Parameters: `columns, rows, *, title, sheet_name, config, metadata, column_types`
- Title block: report title in brand color (14pt bold), metadata key-value pairs (9pt gray), UTC timestamp
- Column headers: brand-colored background, white bold text, centered, wrap text, freeze panes below
- Data rows: Arial 10pt, alternating stripes (`#f8f9fa`), subtle bottom borders
- Auto-detect column types via `_detect_column_types()`:
  - Name heuristics: amount/total/price → currency, rate/margin → percent, id/tranid → text, date/created → date
  - Data sampling fallback: sample 20 non-null values, ≥70% numeric → number
  - Manual override via `column_types` parameter
- Number formatting: currency `#,##0.00;(#,##0.00);"-"`, integers `#,##0`, percent `0.0%`
- IDs always text (no comma grouping for account numbers like 4000)
- Nulls → write empty string `""` to cell (not Python `None`, not literal "None"). This ensures the cell exists in the worksheet but appears blank.
- Auto-fit column widths: sample header + first 50 rows, clamp between 8 and 45 chars
- Print setup: landscape, fit to width, header row repeats
- Footer: row count in 9pt italic gray

**Helper functions:**
- `_humanize_header(col)`: snake_case/camelCase → Title Case
- `_detect_column_types(columns, rows)`: name heuristics + data sampling
- `_write_typed_cell(cell, value, col_type)`: type-appropriate value + format
- `_to_number(value)`: coerce strings to numbers, handles accounting parens

### New router: `backend/app/api/v1/exports.py`

Three endpoints. All use `Annotated[Type, Depends()]` pattern per CLAUDE.md.

**`POST /api/v1/exports/excel`** — Direct data export
```python
@router.post("/excel")
async def export_excel(
    request: ExcelExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
```
- Request: `ExcelExportRequest { columns: list[str], rows: list[list[Any]], title?: str, metadata?: dict, column_types?: dict }`
- 50K row cap (400 if exceeded)
- Auth: `get_current_user` (any authenticated user)
- Audit: `export.excel` event logged
- Returns: `StreamingResponse` with `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- Filename: sanitized title + `.xlsx`

**`POST /api/v1/exports/query-export`** — Re-execute query + export
```python
@router.post("/query-export")
async def query_export(
    request: QueryExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
```
- Request: `QueryExportRequest { query_text: str, title?: str, format: Literal["xlsx", "csv"], metadata?: dict, column_types?: dict }`
- Validates query is read-only (`is_read_only_sql()`)
- **Connection resolution pattern** (same as `netsuite_suiteql.py` lines 374-376):
  ```python
  from app.services.netsuite_oauth_service import get_valid_token
  from app.services.connection_service import get_active_connection
  from app.core.encryption import decrypt_credentials

  connection = await get_active_connection(db, tenant_id=user.tenant_id, provider="netsuite")
  if not connection:
      raise HTTPException(status_code=400, detail="No active NetSuite connection. Connect your NetSuite account first.")
  access_token = await get_valid_token(db, connection)
  creds = decrypt_credentials(connection.encrypted_credentials)
  account_id = creds.get("account_id")
  result = await execute_suiteql_via_rest(access_token, account_id, query_text, limit=50_000, paginate=True, timeout_seconds=120)
  ```
- Row cap: 50K (same as direct export — keeps memory bounded)
- Audit: `export.query_excel` or `export.query_csv` event logged
- Returns: `StreamingResponse` with appropriate content type

**`GET /api/v1/exports/{file_name}`** — Download previously generated export
```python
@router.get("/{file_name}")
async def download_export(
    file_name: str,
    user: Annotated[User, Depends(get_current_user)],
):
```
- Auth required (`get_current_user`) — exported data is sensitive
- Serves files from `EXPORT_DIR` (default `/tmp/exports`)
- Security: rejects filenames containing `..` or `/` (400)
- 404 if file doesn't exist
- Auto-detects content type from extension (`.csv` → `text/csv`, `.xlsx` → xlsx mime type)

### Router registration

In `backend/app/api/v1/router.py`:
```python
from app.api.v1.exports import router as exports_router
api_router.include_router(exports_router)
```

### Pagination fix (Phase 0)

**`backend/app/mcp/tools/netsuite_suiteql.py`** — In `execute()`, OAuth 2.0 path:
- Pass `paginate=True` to `execute_suiteql()` call
- This enables the existing pagination loop in `execute_suiteql_via_rest()`

**`backend/app/workers/tasks/suiteql_export.py`** — Celery CSV export:
- Replace manual OFFSET pagination loop with single call to `execute_suiteql_via_rest(paginate=True)`
- Continue using `loop.run_until_complete()` to wrap the async call (Celery tasks are synchronous)
- Strip trailing `FETCH FIRST ... ROWS ONLY` from query before re-executing using:
  ```python
  import re
  base_query = re.sub(r'\s+FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*$', '', query_text.rstrip().rstrip(';'), flags=re.IGNORECASE)
  ```
  Note: anchored to end-of-string (`$`) to avoid stripping FETCH FIRST in subqueries.

## Frontend

### `apiClient` — Add `download()` method

Add a `download()` method to `frontend/src/lib/api-client.ts` following the same pattern as `stream()` — returns raw `Response` with 401 refresh retry logic. This avoids raw `fetch()` and maintains auth consistency.

```typescript
// In api-client.ts, alongside streamRequest:
async function downloadRequest(path: string, body?: unknown): Promise<Response> {
  // Same auth + 401 retry logic as streamRequest
  // Returns raw Response so caller can call .blob()
}

export const apiClient = {
  // ...existing methods
  download: (path: string, body?: unknown) => downloadRequest(path, body),
};
```

### New hook: `frontend/src/hooks/use-excel-export.ts`

```typescript
interface UseExcelExport {
  exportToExcel: (params: ExcelExportParams) => Promise<void>;
  exportFromQuery: (params: QueryExportParams) => Promise<void>;
  isExporting: boolean;
}
```

- `exportToExcel()`: Uses `apiClient.download()` to POST `columns + rows` to `/api/v1/exports/excel`, creates blob URL, triggers browser download
- `exportFromQuery()`: Uses `apiClient.download()` to POST `query_text` to `/api/v1/exports/query-export`, same blob download
- Returns loading state via `isExporting`

### `DataFrameTable` — Add Excel export button

- Add `FileSpreadsheet` icon button in header bar, next to Copy and CSV
- Smart routing: if `data.truncated && queryText`, use `exportFromQuery()`. Otherwise `exportToExcel()`.
- Shows `Loader2` spinner while exporting
- Passes `queryText` as title (first 80 chars)

### `SuiteQLToolCard` — Add Excel export button

- Add `FileSpreadsheet` button in footer area, next to "Save to Analytics"
- Same smart truncation check as DataFrameTable
- Passes `userQuestion` as title (first 80 chars)

### `QueryPreviewModal` — Add Excel + fix CSV download

- Add "Export Excel" button alongside existing CSV export
- Fix CSV download URL: change from `/api/v1/skills/exports/{fileName}` to `/api/v1/exports/{fileName}`
- Note: this is a coordinated frontend+backend change. The old endpoint never worked (not implemented), so this is not a breaking change — it's fixing a broken flow. No backward compatibility shim needed.
- Both buttons share loading/error states

## Testing

### Unit tests: `backend/tests/test_excel_export_service.py`

| Test | Assertion |
|------|-----------|
| `test_basic_export` | columns + rows → valid .xlsx readable by openpyxl |
| `test_currency_detection` | "amount" column has `#,##0.00` format |
| `test_id_stays_text` | "internalid" column stays text, no comma grouping |
| `test_negative_numbers` | Accounting parens format `(1,234.56)` |
| `test_null_handling` | None → cell value is `""` (empty string, not None, not "None") |
| `test_empty_rows` | Empty rows list → no crash, valid xlsx |
| `test_large_dataset` | 10K rows → valid xlsx (no time assertion — CI speed varies) |
| `test_column_width_clamping` | Width between 8 and 45 |
| `test_metadata_block` | Title, metadata dict, timestamp present in cells |
| `test_column_type_override` | Manual `column_types` overrides auto-detect |
| `test_humanize_header` | snake_case and camelCase → Title Case |
| `test_percent_detection` | "rate" column formatted as `0.0%` |
| `test_stripe_pattern` | Even rows have stripe fill |

### Integration tests: `backend/tests/test_exports_api.py`

| Test | Assertion |
|------|-----------|
| `test_export_excel_returns_xlsx` | 200, content-type is xlsx |
| `test_export_excel_requires_auth` | 401 without token |
| `test_export_excel_audit_logged` | audit event created |
| `test_export_excel_50k_limit` | 400 when rows > 50K |
| `test_query_export_validates_readonly` | 400 for INSERT/UPDATE/DELETE |
| `test_query_export_no_connection` | 400 with "No active NetSuite connection" message |
| `test_download_serves_file` | 200, returns file content |
| `test_download_requires_auth` | 401 without token |
| `test_download_404_missing` | 404 for nonexistent file |
| `test_download_rejects_traversal` | 400 for `../etc/passwd` |

### Pagination tests (in existing test files)

| Test | Assertion |
|------|-----------|
| `test_execute_passes_paginate_true` | `execute_suiteql` called with `paginate=True` |
| `test_celery_export_delegates_pagination` | Uses `execute_suiteql_via_rest(paginate=True)` |

## Data Flow

```
User clicks "Export Excel" on any result card
    ↓
Truncated? → POST /exports/query-export { query_text, format: "xlsx" }
    ↓ (re-executes with full pagination, up to 50K rows)
Not truncated? → POST /exports/excel { columns, rows }
    ↓ (uses data already in frontend)
excel_export_service.generate_excel() → openpyxl → styled .xlsx in BytesIO
    ↓
StreamingResponse → Content-Disposition: attachment → browser download
```

## Files Changed

| File | Change |
|------|--------|
| `backend/pyproject.toml` | Add `openpyxl` dependency |
| `backend/app/services/excel_export_service.py` | **New** — Core Excel formatter |
| `backend/app/api/v1/exports.py` | **New** — Export endpoints (3 endpoints) |
| `backend/app/api/v1/router.py` | Register exports router |
| `backend/app/mcp/tools/netsuite_suiteql.py` | Wire `paginate=True` in OAuth 2.0 path |
| `backend/app/workers/tasks/suiteql_export.py` | Delegate to paginated REST call |
| `frontend/src/lib/api-client.ts` | Add `download()` method |
| `frontend/src/hooks/use-excel-export.ts` | **New** — Excel export hook |
| `frontend/src/components/chat/data-frame-table.tsx` | Add Excel export button |
| `frontend/src/components/chat/suiteql-tool-card.tsx` | Add Excel export button |
| `frontend/src/components/analytics/QueryPreviewModal.tsx` | Add Excel button + fix CSV URL |
| `backend/tests/test_excel_export_service.py` | **New** — 13 unit tests |
| `backend/tests/test_exports_api.py` | **New** — 10 integration tests |

## Open Questions (Resolved)

- **Copy as Excel-pasteable?** Already exists — DataFrameTable's Copy button outputs TSV.
- **Store exports for re-download?** No — generate fresh each time. Simpler, no cleanup needed.
- **Celery for >50K?** Not now. 50K cap is sufficient. Revisit if users need larger exports.
- **CORS for blob downloads?** Existing CORS config allows all `*.vercel.app` origins. `Content-Disposition` is a response header set by the server, not read by JavaScript — the browser handles the download natively from the blob URL. No CORS issue.
