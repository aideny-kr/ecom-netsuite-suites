# Inventory Aging Weekly — report, Excel, PDF, Drive delivery (Slice 1) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic `inventory_aging` report playbook over the BigQuery snapshot for three stock locations, rendered as in the approved mock, with an Excel workbook of every SKU, a PDF of the page, and delivery of both to the tenant's Google Drive.

**Architecture:** Playbook + computation module in `services/report/`; workbook builder shared with the evidence pack; WeasyPrint PDF renderer; a Drive delivery service over the tenant's Google service-account connector; one `deliver` route. No scheduler in this slice (Slice 2 owns cadence).

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Decimal arithmetic, openpyxl, WeasyPrint, Google Drive API v3 (existing google client libs), pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md` Part A (binding). Mock: `/Users/aidenyi/.claude/jobs/bc60c23d/tmp/inventory-aging-weekly-mock.html` (real tenant numbers; read it, never copy it into the repo).

## Global Constraints

- TDD: failing test first, red run in the report. Every computed figure is `Decimal` server-side; no float arithmetic on money; presentation rounding only at render.
- No LLM writes a number; the playbook's watch items, highlights, and narrative are deterministic templates (spec §A2) with named threshold constants.
- The report renderer stays self-contained (inline SVG, CSS-only interactivity; `report_html._CSS` is %-formatted — double every `%`). Nothing truncated: the full aged list is collapsible in-app and appendix pages in the PDF.
- Fixtures are synthetic (fake locations/SKUs/values); never paste the mock's real rows into tests.
- Drive access only through the tenant's `google_sheets` service-account `McpConnector`; no connector → `DeliveryUnavailable`, never 500. Audit event BEFORE the upload with the idempotency key; same-named file is updated, never duplicated.
- Never amend; one commit per logical change; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; stage only your files.
- Commands: `cd backend && DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_ns_flowpages_verify /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest <files> -q`; `…/.venv/bin/ruff check app tests && …/.venv/bin/ruff format --check app tests`. Alembic migrations are applied to the LOCAL verify DB only (`…/.venv/bin/alembic upgrade head` from `backend/` with that DATABASE_URL), never to Supabase/staging.

---

### Task 1: Aging computations + playbook sources

**Files:**
- Create: `backend/app/services/report/inventory_aging.py`
- Modify: `backend/app/services/report/playbooks.py` (register `inventory_aging`: params, sources, sections)
- Test: `backend/tests/report/test_inventory_aging.py` (create `backend/tests/report/` if absent; mirror where other report tests live)
- Read first: `playbooks.py` (`PLAYBOOKS`, `build_playbook_recipe`, `compose_playbook_report`), `refresh_service.py` (`recipe_json` contract: sources `{tool, params, connection_id}`), the 8/25 recipe shape in spec §A1, `backend/app/mcp/tools/` for the `bigquery_sql` tool's params (`query`, `max_rows`), the report-design rule.

**Interfaces:**
- Produces: `build_sources(params) -> dict[str, Source]` (`r_items`, `r_prior`, `r_trend`, `r_meta`; SQL from spec §A7 with validated literal substitution), `compute(payloads: dict[str, list[dict]], params) -> AgingReport` (frozen dataclasses: `LocationSummary`, `BucketRow`, `TopItem`, `TrendPoint`, `KpiCard`, `WatchItem`, `Highlight`, `Narrative`, `Provenance`), constants `WATCH_VALUE_THRESHOLD = Decimal("50000")`, `WATCH_SHARE_THRESHOLD_PTS = Decimal("1.0")`, `BUCKETS = ("0-30","31-60","61-90","91-180","180+")`.

**Steps:**
- [ ] Failing tests with a synthetic 3-location fixture: bucket assignment at the boundaries (30/31, 90/91, 180/181); location totals equal bucket sums; all-location totals equal the sum of locations; share = aged/total to 1 dp; prior-week deltas and Δ pts; trend points ordered oldest→newest; top-5 per location by value with "top 5 = N%" share; watch-item rules fire exactly at the thresholds and not below; highlight ordering (largest mover first); narrative slots filled and deterministic across two runs; `Decimal` everywhere (assert no `float` in outputs); locations validated (unknown location → `ValueError`); SQL substitution rejects a location containing a quote.
- [ ] Run red; implement; run green; ruff.
- [ ] Commit: `feat(report): inventory aging computations and playbook sources`.

---

### Task 2: Render the report as the mock

**Files:**
- Modify: `backend/app/services/report/report_html.py` (new section renderers: watch items, KPI cards with sparklines, trend line chart, variance table, bucket table, top positions + collapsible full list, highlights, narrative — reuse existing renderers where they exist), `playbooks.py` (sections list)
- Test: `backend/tests/report/test_inventory_aging_render.py`
- Read first: the existing renderers in `report_html.py` (statement tables, provenance block, `_CSS`), the mock file (structure, labels, copy, colours), `.claude/rules/report-design.md`.

**Interfaces:**
- Consumes: `AgingReport` from Task 1.
- Produces: section types `watch_items`, `kpi_cards`, `trend_chart`, `variance_table`, `bucket_table`, `top_positions`, `highlights`, `narrative` (names final; Slice 2's `report.compose` step only names the playbook).

**Steps:**
- [ ] Failing tests: rendered HTML contains the mock's section headings and labels verbatim ("Watch items", "Aged share of on-hand value, by location", "By location", "Aging buckets by location", "Largest aged positions", "Highlights", "Narrative", "Sources & method"); KPI cards carry ▲/▼ and favourable classes per the rules; the trend chart is inline SVG with one polyline per location and endpoint labels; the bucket table has Current/Aged subtotal rows and an On hand total row; the full aged list is present (collapsed) and complete (row count = fixture); negative deltas render in parentheses; no `<script>` tags; `@media print` un-clips the collapsible list; a literal `%` in `_CSS` is doubled (existing test pattern).
- [ ] Implement; render the fixture to `/private/tmp/claude/inventory-aging-fixture.html` and LOOK at it against the mock (this is the acceptance gate); adjust until the layout matches.
- [ ] Commit: `feat(report): render the inventory aging report as the approved mock`.

---

### Task 3: Excel workbook builder

**Files:**
- Modify: `backend/app/services/reconciliation/evidence_service.py` (extract/add `build_workbook(sheets: list[SheetSpec]) -> io.BytesIO`; the evidence pack keeps byte-identical output)
- Create: `backend/app/services/report/report_excel.py` (`build_inventory_aging_workbook(report: AgingReport) -> io.BytesIO`)
- Test: `backend/tests/report/test_report_excel.py` (+ the existing evidence tests stay green)
- Read first: `evidence_service.py` (`generate_section_excel`, `escape_csv_injection`, styling), `excel_export_service.py`.

**Steps:**
- [ ] Failing tests: seven sheets in the spec's order and names; header row frozen + autofilter on every data sheet; a cell value starting with `=`/`+`/`-`/`@` is prefixed with `'`; numbers are numeric cells, dates are date cells; per-location sheet row count = fixture; Aged 90+ sheet = union of aged rows; Method sheet lists the four sources; sheet names ≤ 31 chars.
- [ ] Implement; run this file + the evidence-pack tests; ruff.
- [ ] Commit: `feat(report): inventory aging Excel workbook on a shared sanitised workbook builder`.

---

### Task 4: PDF renderer (WeasyPrint)

**Files:**
- Modify: `backend/pyproject.toml` (add `weasyprint`), `backend/Dockerfile` (system libs: `libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info fonts-dejavu-core` or the equivalents for the base image), `backend/app/services/report/report_pdf.py` (create)
- Test: `backend/tests/report/test_report_pdf.py`
- Read first: the Dockerfile base image and its package manager; how `rendered_html` is produced; `.claude/rules/deploy.md` (Dockerfile changes are T2 infra).

**Steps:**
- [ ] Failing test: `render_report_pdf(html)` returns bytes starting with `%PDF`; for the aging fixture with an appendix the page count ≥ 2 (parse with `pdfplumber`, already a dependency); a `<script>` in the HTML is ignored.
- [ ] Install the dependency in the venv for tests (`uv`/`pip` per the repo's lock workflow; commit the lock change); implement; verify the Dockerfile builds locally if Docker is available, else note it for the deploy step.
- [ ] Commit: `feat(report): server-side PDF rendering with WeasyPrint`.

---

### Task 5: Drive delivery service + route + migration

**Files:**
- Create: `backend/app/services/report/report_delivery.py`, `backend/alembic/versions/099_report_delivery_json.py`
- Modify: `backend/app/models/report.py` (`delivery_json`), `backend/app/api/v1/reports.py` (`POST /reports/{id}/deliver`; `delivery_json` in the read model), `backend/app/schemas/report.py`
- Test: `backend/tests/report/test_report_delivery.py`, `backend/tests/api/test_reports_deliver_api.py`
- Read first: `api/v1/drive_folders.py::_sheets_connector` (connector lookup + `decrypt_credentials`), `services/docs_service.py` (Drive multipart upload pattern), `services/sheets_service.py` (`shared_drive_id`), the audit event service, the reports routes' permission dependencies, `.claude/rules/alembic.md`.

**Interfaces:**
- Produces: `deliver_report_to_drive(db, *, tenant_id, report_id, actor_type, actor_id, period_key) -> DeliveryResult`, exceptions `DeliveryUnavailable` (no connector) and `DeliveryFailed`; `DriveClient` protocol (`find_folder`, `create_folder`, `find_file`, `upload_new`, `update_existing`) with a real implementation and a fake for tests.

**Steps:**
- [ ] Failing tests: no connector → `DeliveryUnavailable`; folder found-or-created once; first delivery uploads two files; a second delivery for the same `period_key` UPDATES both (no new file ids); audit event `report.delivery.started` recorded before any upload call (assert ordering with the fake); `delivery_json`, `published_drive_url`, `published_at` written on success; a failing upload → `report.delivery.failed` and no partial `delivery_json`; route: 401/403 gates, 404 unknown report, 200 with the result, 409 when unavailable (clear detail).
- [ ] Migration 099 (down_revision `098_celigo_flow_errors_checked`, revision id ≤ 32 chars), applied to the local verify DB only.
- [ ] Implement; run the tests + `tests/api/test_reports*.py`; ruff.
- [ ] Commit: `feat(report): deliver a report to Google Drive as PDF + Excel (idempotent, audited)`.

---

### Task 6: Compose the Framework report headlessly + frontend delivery line

**Files:**
- Create: `backend/scripts/compose_inventory_aging.py`
- Modify: `frontend/src/app/(dashboard)/reports/**` (the report page: a "Delivered to Drive · PDF · XLSX" line when `delivery_json` is present) and the report type in `frontend/src/hooks/` (+ tests)
- Read first: `compose_playbook_report` signature, how other scripts get a session and tenant context (`set_tenant_context`), the report page component and its test.

**Steps:**
- [ ] Failing tests: the script's `main(tenant_id, locations)` composes a report titled "Inventory Aging Weekly" with `auto_refresh="off"` and `dashboard_pinned_at` set (DB-backed test with the `db` fixture, sources patched to the synthetic payloads); the frontend line renders only with `delivery_json` and links both files.
- [ ] Implement; run backend + frontend tests (`cd frontend && npx vitest run src/app src/components/reports src/hooks && npx tsc --noEmit -p tsconfig.json`).
- [ ] Commit: `feat(report): headless compose of Inventory Aging Weekly + delivered-to-Drive line`.

---

## Verification (after Task 6)

`scripts/verify.sh --full`; frontend suites; the rendered fixture viewed against the mock (Task 2's gate); T2 multi-angle gate on the backend (PDF/Dockerfile + delivery); staging: build the backend image with WeasyPrint, compose the Framework report, `POST /deliver`, open the Drive folder and the PDF; compare the live page with the mock.
