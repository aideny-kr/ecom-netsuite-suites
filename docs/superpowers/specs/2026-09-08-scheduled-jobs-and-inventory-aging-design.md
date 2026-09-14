# Scheduled Jobs + Inventory Aging Weekly — design

**Date:** 2026-09-08 · **Status:** approved by the operator ("approved, build it … keep it as close to the mock") · **Binding mocks:**
- Report: artifact https://claude.ai/code/artifact/02f2824c-b2cc-4a64-a781-876d1a999a73 (file `/Users/aidenyi/.claude/jobs/bc60c23d/tmp/inventory-aging-weekly-mock.html`; uses REAL tenant numbers → never committed)
- Scheduled Jobs page: artifact https://claude.ai/code/artifact/829383f3-ea89-4607-b4c1-8c518d15f9fd (file `/Users/aidenyi/.claude/jobs/bc60c23d/tmp/scheduled-jobs-mock.html`)

The mocks are the acceptance reference: sections, layouts, labels, and copy are reproduced as drawn; the slice is done when the rendered page holds up against the mock, not when tests pass (`.claude/rules/report-design.md` §1–2).

## 0. Decisions (each binding)

1. **One report, three locations (Dimerco, Fedex, Panurgy), weekly Monday 06:00 America/Los_Angeles**, delivered to Google Drive as a PDF of the report plus an Excel workbook with every SKU. "Virtual" is excluded (not a stock location) and named as such in the method block.
2. **Age = days since last restock**, inferred from the daily BigQuery snapshots (a restock is any day a SKU's quantity rose versus the previous day, or its first appearance) — the 8/25 chat's method, kept because SuiteQL exposes no lot/receipt dates. Buckets 0–30 / 31–60 / 61–90 / 91–180 / 180+.
3. **A scheduled job is an instruction, compiled into a plan, replayed deterministically.** The agent runs at creation and on instruction edits, never at run time. Runs replay the approved plan; every number comes from tools. Plan changes are approved by a person (diff shown) before the next run; "Run once with this change" exists and records the plan version used.
4. **Steps come from a registry allow-list** with READ/WRITE tags. NetSuite and Celigo writes are not in the registry in this program; the plan panel says so.
5. **Every run ends with a reason** (`done | budget | stall | error | blocked`), has a cost budget, a `jobs` row (correlation id, plan version, what it read/wrote), and audit events. WRITE steps carry an idempotency key (job + period key) and write their side-effect audit event before the call.
6. **Missed runs catch up once, never twice.** Failure: retry once after 15 minutes, then pause the job and notify the owner; previous outputs stay.
7. **System jobs (Beat entries) are shown read-only** on the same page so nothing runs invisibly.
8. **Chat can create a job** ("schedule this every Monday") but it lands on the page for the same compile-and-approve step.
9. **PDF via WeasyPrint** in the backend image (print stylesheet + inline SVG, no browser). Excel via the evidence-pack writer's sanitised cells, extended to multi-sheet workbooks.
10. **Tiers:** report playbook + Excel = T1; PDF (Dockerfile change), Drive delivery, the schedules executor and compiler = T2.

Out of scope now: NetSuite/Celigo writes from a schedule; email delivery (no email step until an email service exists on main); editing a compiled plan by hand (the instruction is the source of truth).

---

## Part A — Inventory Aging Weekly (Slice 1, branch `feat/inventory-aging-report-delivery`)

### A1. Playbook `inventory_aging` (`backend/app/services/report/playbooks.py` + a new `backend/app/services/report/inventory_aging.py` for the computations)

Params: `locations: list[str]` (default `["Dimerco","Fedex","Panurgy"]`), `compare_days: int = 7`, `trend_weeks: int = 9`, `snapshot_date: date | None` (default: latest per location). Sources (all `bigquery_sql`, `connection_id: None`, exactly like the 8/25 recipe `a938ebfa…`), one per question so the refresh replays them unchanged:

| id | returns |
|---|---|
| `r_items` | every on-hand SKU per location on the latest snapshot: location, sku, item_desc, category, qty_on_hand, inventory_amount, snapshot_date, last_restock_date, days, bucket |
| `r_prior` | the same aggregate per location on the snapshot `compare_days` earlier: skus, qty, value, skus_90p, qty_90p, value_90p, value_180p |
| `r_trend` | per location, `trend_weeks` weekly points ending at the latest snapshot: d, total_value, value_90p, pct_90p |
| `r_meta` | per location: first/last snapshot date, snapshot count |

Bucket, location, and all-location aggregates are computed server-side from `r_items` with `Decimal` (never in the LLM, never in the browser). The SQL is the one used for the mock (appendix §A7), parameterised by locations and dates.

Sections, in the mock's order and with the mock's labels: report head (title `Inventory Aging — Week of {snapshot date}`, sub-line, meta block incl. "no model generated a figure"); **Watch items** (computed, gated at $50K or 1 pt of share; rule set in §A2); **four KPI cards** (On-hand value · all 3 / Aged > 90 days · value / Aged share of value / Aged > 180 days · value; each: value, Δ vs prior with ▲▼ and favourable colour, sub-detail, 9-point sparkline with emphasised endpoint); **trend chart** (line, aged share % per location, same scale, endpoint labels, legend); **By location** variance table (On-hand $ | Prior wk | Δ $ | Δ % | Aged > 90 $ | Share | Δ pts, All-locations total row, note line); **Aging buckets by location** table (per location value / % of location / units / SKUs; Current (≤ 90) and Aged (> 90) subtotal rows; On hand total row; bucket swatches); **Largest aged positions** (top 5 per location with "top 5 = N%" group rows; the FULL aged list in a collapsible block in-app and as appendix pages in the PDF — never truncated); **Highlights** (driver-attribution bullets, §A2); **Narrative** (deterministic two-paragraph template, §A2); **Sources & method** (provenance block with source, snapshots used, age definition, query count + executed_at + bytes scanned, integrity checks).

Favourable/unfavourable (IBCS, red/green only here): lower aged value, lower aged share, lower 180+ value are favourable; higher on-hand value is favourable (as drawn). Parentheses for negative deltas in tables; `tabular-nums`; whole dollars in tables, `$22.70M`/`$867.8K` style in cards and prose.

### A2. Computed insight rules (deterministic; every threshold a named constant)

- Watch items (max 4, ordered by |impact|): (1) any location whose aged share moved ≥ 1.0 pt — text "{loc} 90+ day share {x}%, up/down {d} pts in a week: aged value ±$… while total on-hand ±$…"; (2) any location whose aged value moved ≥ $50K — "{loc} aged value $…, down/up $… (±%) as N SKUs left/entered the 90+ buckets"; (3) the location with the highest aged share, with weeks-running count when ≥ 2 — "…for the Nth week running"; (4) the 180+ line — "180+ days: $… across N SKUs — x% of aged value, N SKUs more/fewer than a week ago". Dot colour: unfavourable red, favourable green, "highest share" amber, informational grey.
- Highlights (up to 6): the denominator/numerator attribution when share and total move in opposite directions; the largest aged-value mover with SKU counts; concentration ("Two laptop SKUs make up x% of {loc}'s aged value: …"); the persistent-high-share statement (weeks above a level); the trailing-range statement for the all-location aged value; the 180+ breadth statement (value, SKUs, median position).
- Narrative: two fixed paragraphs with slots, as in the mock.

### A3. Excel workbook (`backend/app/services/report/report_excel.py`)

`build_inventory_aging_workbook(payload) -> io.BytesIO` with sheets **Summary** (KPIs + by-location table), **Buckets** (the bucket table), one sheet per location (columns: SKU, Item, Category, Units, Value, Days since restock, Bucket, Last restock, Snapshot), **Aged 90+** (all locations), **Method** (sources & queries). Built on a new generic `build_workbook(sheets: list[SheetSpec]) -> io.BytesIO` in `backend/app/services/reconciliation/evidence_service.py` (or a sibling module both import) that applies `escape_csv_injection` to every string cell, freezes the header row, sets autofilter, writes numbers as numbers and dates as dates, and caps sheet names at 31 chars. The evidence pack keeps its byte-identical output (its tests stay green).

### A4. PDF (`backend/app/services/report/report_pdf.py` + `backend/Dockerfile` + `pyproject.toml`)

`render_report_pdf(rendered_html: str, *, appendix_html: str | None = None) -> bytes` using WeasyPrint. The report renderer's `@media print` rules un-clip scroll regions; the appendix (full aged list) is appended as its own pages. Dockerfile adds the WeasyPrint system libraries (pango, cairo, gdk-pixbuf, libffi, fonts). Smoke test: output starts with `%PDF` and has ≥ 2 pages for the aging report fixture.

### A5. Drive delivery (`backend/app/services/report/report_delivery.py`)

`deliver_report_to_drive(db, *, tenant_id, report_id, actor_type, actor_id, period_key) -> DeliveryResult(pdf_file_id, pdf_url, xlsx_file_id, xlsx_url, folder_id, delivered_at)`:
- credentials: the tenant's Google service-account `McpConnector` (`provider == "google_sheets"`, same lookup as `api/v1/drive_folders.py::_sheets_connector`); no connector → `DeliveryUnavailable` (the run ends `blocked`, never 500).
- folder: `Reports / <report series title>` under the connector's `shared_drive_id` (or My Drive root), found or created by name.
- files: `<report title> — <snapshot date>.pdf` and `.xlsx`; a same-named existing file is UPDATED (idempotent replace keyed by `period_key`), never duplicated.
- side effects: an `AuditEvent` `report.delivery.started` (with the idempotency key) BEFORE the upload, `report.delivery.completed|failed` after; `reports.published_drive_url` (pdf) + `published_at` + new `delivery_json` (`{pdf: {file_id,url}, xlsx: {file_id,url}, folder_id, period_key, delivered_at}`) written on success (migration `099_report_delivery_json`).
- `POST /api/v1/reports/{id}/deliver` (report permissions as the existing settings route) runs it now and returns `DeliveryResult`; `GET /reports/{id}` exposes `delivery_json`. The in-app report page shows a "Delivered to Drive" line with the two links when present (small; the report page mock is not part of this slice).

### A6. The Framework report

A management command / script `backend/scripts/compose_inventory_aging.py --tenant <id>` composes the playbook headlessly (actor_type system), pins it to the dashboard, `auto_refresh = "off"` (Part B's schedule owns the cadence), title **Inventory Aging Weekly**. Live check on staging: the rendered page against the mock; `POST …/deliver` puts both files in Drive.

### A7. SQL appendix (parameterised; the mock's queries)

Items (`r_items`), per location `L`, latest snapshot `D`:
```sql
WITH daily AS (
  SELECT location, sku, snapshot_date, qty_on_hand,
         LAG(qty_on_hand) OVER (PARTITION BY location, sku ORDER BY snapshot_date) AS prev_qty
  FROM `frameworkreporting.inventory_snapshot` WHERE location IN UNNEST(@locations)),
restocks AS (SELECT location, sku, snapshot_date AS restock_date FROM daily WHERE prev_qty IS NULL OR qty_on_hand > prev_qty),
last_restock AS (SELECT location, sku, MAX(restock_date) AS last_restock_date FROM restocks GROUP BY location, sku),
latest AS (SELECT location, MAX(snapshot_date) AS d FROM `frameworkreporting.inventory_snapshot` WHERE location IN UNNEST(@locations) GROUP BY location),
cur AS (SELECT s.location, s.sku, s.item_desc, s.category, s.qty_on_hand, s.inventory_amount, s.snapshot_date
        FROM `frameworkreporting.inventory_snapshot` s JOIN latest l ON l.location = s.location AND l.d = s.snapshot_date WHERE s.qty_on_hand > 0)
SELECT c.*, r.last_restock_date, DATE_DIFF(c.snapshot_date, r.last_restock_date, DAY) AS days,
       CASE WHEN DATE_DIFF(c.snapshot_date, r.last_restock_date, DAY) <= 30 THEN '0-30' WHEN … <= 60 THEN '31-60' WHEN … <= 90 THEN '61-90' WHEN … <= 180 THEN '91-180' ELSE '180+' END AS bucket
FROM cur c LEFT JOIN last_restock r USING (location, sku)
```
`r_prior` = the same aggregate at `latest.d - compare_days`; `r_trend` = the `pct_90p` series at weekly points (`MOD(rn-1,7)=0`, `rn <= 7*trend_weeks`) as run for the mock; `r_meta` = min/max/count of snapshot dates per location. The `bigquery_sql` tool takes the final SQL string (parameters substituted server-side with validated literals; locations are validated against the snapshot's distinct locations before substitution).

---

## Part B — Scheduled Jobs platform (Slice 2, branch `feat/scheduled-jobs-platform`, after Slice 1 merges)

### B1. Data (migration `100_scheduled_jobs`)

Extend `schedules` (`backend/app/models/pipeline.py::Schedule`): `instruction TEXT`, `plan_json JSONB`, `plan_version INT default 0`, `plan_status TEXT` (`draft|pending_approval|approved`), `pending_plan_json JSONB`, `pending_plan_reason TEXT`, `timezone TEXT default 'UTC'`, `delivery_json JSONB`, `budget_json JSONB` (`{bytes_scanned, seconds, usd}`), `catch_up TEXT default 'once'` (`once|skip`), `owner_id UUID`, `last_run_at`, `last_run_status TEXT`, `next_run_at`, `paused_at`, `pause_reason TEXT`. `schedule_type` stays (`"job"` for instruction jobs). `is_active` stays. Cron in `cron_expression` (5-field), evaluated in `timezone`.

### B2. Step registry (`backend/app/services/jobs/registry.py`)

`STEP_REGISTRY: dict[str, StepSpec]` — `StepSpec(type, label, kind: "read"|"write", params_schema: dict, executor: Callable, idempotency: Callable | None)`. v1 types: `bigquery_sql` (read), `report.compose` (read; playbook + params, or refresh of an existing report id), `report.render_pdf` (read), `report.build_xlsx` (read), `drive.upload` (write; files from earlier steps → folder; idempotency key `job:{id}:period:{period_key}`), `recon.run` (read; the existing scheduled recon task with a window; needs-review lines are never auto-approved). Anything else is rejected at compile and at run (two choke points). The registry is the ONLY place a step type is defined; the compiler's output schema is generated from it.

### B3. Compiler (`backend/app/services/jobs/compiler.py`)

`compile_instruction(db, *, tenant_id, instruction, actor_id) -> CompiledPlan | Clarification`. One LLM call through the chat's adapter/BYOK routing with a structured-output schema derived from the registry (step type enum + per-type params), plus the tenant's context (connections available, locations known from the snapshot, existing reports). The result is VALIDATED against the registry after the call (types, params, and that every `drive.upload` refers to files produced by earlier steps); invalid → one repair round, then `Clarification`. `Clarification(question)` is returned when a required param cannot be derived from the instruction (the mock's "which subsidiary" case). `plan_diff(old, new) -> list[DiffLine]` for the pending-change panel. Every compile writes an audit event (instruction hash, plan version, model).

### B4. Executor (`backend/app/workers/tasks/scheduled_jobs.py`)

Beat entry `scheduled-jobs-sweep` every minute → `scheduled_jobs_sweep_all` (fan out per active tenant, like `report_auto_refresh_all`) → `run_due_jobs(tenant_id)`: `SELECT … FOR UPDATE SKIP LOCKED` on due schedules (`next_run_at <= now`, active, approved plan, not paused); catch-up = run once if a run was missed; compute `next_run_at` with `croniter` in the schedule's timezone. Each run = one `jobs` row (job_type `scheduled_job`, parameters incl. schedule id + plan version + period key, correlation id) via the instrumented task base; steps execute in order with the run's budget (bytes scanned, seconds, usd) enforced between steps; the run ends with a reason enum stored in `result_summary.reason` (`done|budget|stall|error|blocked`); on `error`: schedule one retry 15 min later (a `jobs` row with `attempt=2`), then `paused_at` + `pause_reason` + notify the owner (audit event + the existing notification path if one exists on main; otherwise the audit event and the page badge are the notification). WRITE steps: audit `started` with the idempotency key before the call.

### B5. API (`backend/app/api/v1/schedules.py`, permission `schedules.manage`, quota unchanged)

`GET /schedules` (list with last run status/at, next_run_at, kind tags derived from the plan, delivery summary), `GET /schedules/{id}` (full: instruction, plan, pending plan + diff, schedule, delivery, budget), `POST /schedules` (`{name?, instruction, cron_expression?, timezone?, delivery?}` → compiles; returns `201` with `plan_status=pending_approval` and the plan, or `409` with the clarification question), `PATCH /schedules/{id}` (instruction → recompile into `pending_plan_json`; cron/timezone/delivery/budget/catch_up edits apply directly), `POST /schedules/{id}/approve` (pending → approved, version+1), `POST /schedules/{id}/run` (`{use_pending: bool}` → enqueues one run now; records the plan version used), `POST /schedules/{id}/pause`, `POST /schedules/{id}/resume`, `DELETE`, `GET /schedules/{id}/runs` (from `jobs`). `GET /jobs/schedules` (Beat, read-only) stays for the system rows. MCP `schedule_ops.execute_create` → the compile path; `execute_run` implemented (no longer a stub).

### B6. Frontend (`frontend/src/app/(dashboard)/scheduled-jobs/**`, `frontend/src/components/scheduled-jobs/**`, `frontend/src/hooks/use-scheduled-jobs.ts`)

Nav item **Scheduled jobs** (`/scheduled-jobs`, icon `CalendarClock`) between Reports and Settings in `lib/constants.ts` `NAV_ITEMS`. Pages reproduce the mock:
- **List** (state one): page head (title, tenant · timezone, Run history, + New job), four tiles (Jobs with quota, Last 7 days runs, Needs attention, Next run), the table (Job with sub-line, Does with READ/WRITE tags and a one-line summary, Schedule with cron sub-line, Last run pill + when/took, Next, Delivers to, Run now/Resume/Open), system rows from `GET /jobs/schedules` with the `system` pill, the footer hint copy.
- **Job** (state two): head with status pill and Run now / Pause / Duplicate / Delete; **Instruction** panel (text, Edit, "Ask the agent to adjust…" = opens the editor with the agent's clarification thread in v1), the hint copy; **Compiled plan** panel (numbered steps: title, description with `code` chips, guard line, READ/WRITE tag, `allow-listed` pill; the "What the agent may not do here" note verbatim); **Pending change** panel (when `pending_plan_json`: the explanation line, the diff block, Approve · use from next run / Run once with this change / Discard); **Schedule** panel (segmented Hourly/Daily/Weekly/Monthly/Cron, weekday + time, time zone, next run, catch-up, budget); **Delivery** panel; **Runs** panel (When, Took, Ended pill + reason, Outputs; All runs).
- **New job** (state three): step 1 instruction textarea with the hint copy and Compile plan →; step 2 the plan review with the clarification question when returned; then schedule + delivery; Save.
- Query state through `queryState()`; never render a fabricated "0 jobs"; empty state copy: "No scheduled jobs yet. Describe one in plain language, or ask the chat to schedule something."

### B7. First job + live proof

Create **Inventory Aging Weekly** for Framework from the mock's instruction text; compile → the five-step plan of the mock (queries → compose → PDF + Excel → Drive → finish); approve; weekly Monday 06:00 America/Los_Angeles; delivery `Reports / Inventory aging`. Live check on staging: "Run now" produces a new report version and both Drive files, the run row shows `done` with duration and outputs; then the first Monday run.

### B8. Tests and gates

Backend: registry (unknown type rejected at compile and at run), compiler (schema derived from registry; validation after the call; clarification path; diff), executor (due computation in tz, SKIP LOCKED, catch-up once, retry then pause, reason enum, budget stop, idempotency key on writes, audit before write), API (gates, quota, approve/run/pause), MCP run no longer a stub. Seeded-tenant e2e: a due job runs exactly once, a missed one catches up once, a failed one pauses after the retry. Frontend: list/detail/new-job components against the mock's copy. Blocking multi-angle gate on the backend; live run on staging before the first Monday.
