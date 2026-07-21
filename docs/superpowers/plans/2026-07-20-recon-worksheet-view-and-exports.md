# Recon — Worksheet View + Per-Section CSV/Excel Export — Plan

> **For agentic workers:** execute via superpowers:subagent-driven-development, one fresh implementer per task, TDD, spec+quality review per task.
> Operator approved the design mock 2026-07-20 ("this is exactly what we want"): artifact `worksheet-export-mock-v1` — https://claude.ai/code/artifact/ba248c9d-2129-4cfb-8885-23985c69bbd4. **The rendered UI matching that mock is the acceptance gate** (visualize-before-building rule), not green tests alone.

**Goal:** `/reconciliation` resolution surface renders as Excel-style worksheets (column tables with headers), and every section/group is downloadable as CSV or formatted XLSX.

**Architecture:** Pure rendering change on the FE (the API already returns every candidate column) + one new read-only export endpoint reusing the proven `EvidencePackGenerator` (openpyxl) machinery and the evidence endpoint's enrichment join. No migrations, no new flags, no mutation-surface changes — the existing approve/reject/notes/materiality wiring in `page.tsx` is preserved verbatim and re-parented into table rows.

**Tier:** T1 (read-only endpoint + rendering). Reviewers must still explicitly verify: tenant scoping on every query in the export endpoint, feature-gating parity with the evidence endpoint, and that approve/reject wiring survives the re-render byte-for-byte in behavior.

**Branch:** `feat/recon-worksheet-exports`

## Global Constraints

- Gating: export endpoint uses `require_feature("reconciliation")` exactly like `GET /evidence/{run_id}` (reconciliation.py:1195-1199). NO `recon_resolution_ui` flag check — read endpoints are not flag-gated (flag gates the page + the 4 mutation endpoints only; planner/evidence run for all recon tenants).
- Every DB query tenant-scoped (`tenant_id == user.tenant_id` on every table incl. joins — mirror reconciliation.py:1240-1265).
- Decimal only for amounts; serialization follows the existing `proposed_amount` handling in `ResolutionProposalResponse`.
- FE: `apiClient` for JSON; downloads via bare `<a href="/api/v1/...">` mirroring the existing Evidence Pack link (page.tsx:320-327) — same auth story, don't invent a new one.
- FE state keys stay `${group_key}:${currency}` (`cardKey`) — two currencies of one group_key must not share expand/ticked/reset state (page.tsx:344-360 comment).
- Tests: backend via local docker harness ONLY (`cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite" DATABASE_URL_DIRECT="" .venv/bin/python -m pytest …`) — NEVER Supabase. FE via `npx vitest run`.
- Push both remotes; never amend; one commit per logical change.

## Export endpoint contract (Task 2 implements; Tasks 5 consumes)

`GET /reconciliation/runs/{run_id}/export?section=<groups|proposals|results>&format=<csv|xlsx>[&group_key=…][&currency=…][&action=…]`

- `section=groups` — one row per group×currency (the groups worksheet). Columns (CSV header order): `group_key, root_cause, action, booking_vehicle, currency, count, proposed_count, approved_count, above_materiality_count, total_amount`.
- `section=proposals` — item rows; optional filters `group_key`, `currency`, `action` (e.g. `action=needs_human` exports the needs-human section; `group_key`+`currency` exports one group). CSV columns: `order_reference, stripe_charge_id, netsuite_internal_id, netsuite_record_type, stripe_amount, netsuite_amount, variance_amount, proposed_amount, currency, status, above_materiality, root_cause, action, booking_vehicle, narrative`. XLSX adds: `proposal_id, run_id, source, decided_by, decided_at, created_at`.
- `section=results` — classic bucket table export. CSV columns: `match_type, confidence, status, bucket, stripe_amount, netsuite_amount, variance_amount, variance_type, variance_explanation, currency, match_rule` (the evidence "All Results" column set, reconciliation.py:1217-1234).
- `format=csv` → stdlib `csv` into `io.StringIO`, `media_type="text/csv"`. `format=xlsx` → new `EvidencePackGenerator.generate_section_excel(title, headers, rows)` reusing existing header styling (evidence_service.py), `StreamingResponse` with the xlsx media type (mirror reconciliation.py:1297-1299).
- Filename: `recon-{section}-{date_from}-{date_to}[-{group_key}].{ext}`.
- Unknown section/format → 400. Run not found / other tenant → 404 (mirror evidence).
- Data assembly REUSES existing queries: groups = same aggregation as the resolution-summary endpoint; proposals = the evidence endpoint's enrichment join (reconciliation.py:1236-1284) extended with the result's `stripe_amount/netsuite_amount/variance_amount` + `netsuite_record_type`; results = the evidence endpoint's results query. Factor shared helpers if the endpoint bodies would otherwise duplicate >20 lines; do NOT re-invent joins.

## Tasks (TDD each; fresh implementer per task)

### Task 1 — backend: amounts + action filter on the proposals list path
**Files:** `backend/app/api/v1/reconciliation.py` (ResolutionProposalResponse, `_enrich_proposal_response`, list_group_proposals), `backend/tests/` (existing resolution API test module).
- Add `stripe_amount`, `netsuite_amount`, `variance_amount` (Decimal|None) to `ResolutionProposalResponse`, populated from the already-joined `ReconciliationResult` in `_enrich_proposal_response` (reconciliation.py:926-955) — no new query.
- Add optional `action` query filter to the group-proposals list endpoint (it currently filters by group_key; `action=needs_human` must work WITHOUT group_key so the FE can render the cross-group needs-human section in one call). Keep existing callers' behavior unchanged when the param is absent.
- Tests RED first: response carries the three amounts; `action=needs_human` without group_key returns items across groups; absent param unchanged.

### Task 2 — backend: export endpoint
**Files:** `backend/app/api/v1/reconciliation.py` (new endpoint), `backend/app/services/reconciliation/evidence_service.py` (`generate_section_excel`), tests in the existing recon API test module.
- Implement the contract above. Tests RED first: each section returns correct CSV header row + a known seeded row; xlsx returns a workbook with the section sheet (openpyxl load from bytes); group_key/currency/action filters narrow rows; bad section/format → 400; other-tenant run → 404; superseded/rejected proposals excluded (mirror reconciliation.py:1263).

### Task 3 — FE: groups worksheet replaces the card stack
**Files:** create `frontend/src/components/reconciliation/resolution-groups-table.tsx`; modify `frontend/src/app/(dashboard)/reconciliation/page.tsx` (the flag-ON branch, lines ~340-404); delete `resolution-group-card.tsx` once nothing references it; port its tests.
- Two sections per the mock: **"Resolution groups"** (groups where `action !== 'needs_human'`) as a worksheet — columns Group (root-cause label + descriptor) | Action chip | Vehicle chip | CCY | Items | Approved | Above mat. | Total (right-aligned, tabular-nums) | row actions (Approve group / Reject / Review) — with expand-in-place: the expanded row hosts notes textarea + `ResolutionGroupItems` + per-group ExportMenu (Task 5) + the approve/reject controls. **"Needs human review"** as a separate item-level worksheet (uses Task 1's `action=needs_human` cross-group fetch): Order ref | Stripe charge | NetSuite ID | Amount | Root cause chip | Why held (narrative) | Investigate-in-chat button.
- Preserve verbatim: `cardKey` state keying, `tickedAboveByGroup` add/remove, `resetSignal` bump + ticked-clear on approve success, `disabled={!reconEnabled || isRunClosed}`, notes disabled on needs_human, approve disabled when one-click count ≤ 0. The page.tsx callbacks move unmodified; only the presentation component changes.
- Use the shared `frontend/src/components/ui/table.tsx` primitives; wide tables get their own `overflow-x-auto` container.
- Vitest RED first: renders one row per group×currency; expand toggles per cardKey; approve fires with ticked ids and clears on success; needs-human section renders item rows from the action-filtered query.

### Task 4 — FE: items worksheet
**Files:** rework `frontend/src/components/reconciliation/resolution-group-items.tsx` + its test file.
- Replace the `<ul>` narrative list with a table: select-checkbox (above_materiality ∩ status==='proposed' only — semantics unchanged) | Order ref | Stripe charge | NetSuite ID (+record-type suffix) | Stripe amt | NetSuite amt | Variance | Status chip | Materiality chip | Narrative (muted, truncating with title attr). Amount columns from Task 1's new fields; right-aligned, tabular-nums; identifiers keep click-to-copy (port the awaited-clipboard logic from the current IdentifierSegment).
- Vitest RED first: columns render; copy fires clipboard with exact id; checkbox only on eligible rows; Investigate button only on needs_human action.

### Task 5 — FE: ExportMenu + placements
**Files:** create `frontend/src/components/reconciliation/export-menu.tsx`; wire into `resolution-groups-table.tsx` (section header + expanded group + needs-human header), `page.tsx` classic branch next to the bucket table (`section=results`).
- Dropdown (button + popover, match existing dropdown patterns in the codebase): "CSV — visible columns" / "Excel — formatted sheet". Entries are `<a href>` to the export endpoint with the right query string; per-group entry carries `group_key`+`currency`; needs-human carries `action=needs_human`.
- Vitest RED first: hrefs assembled correctly per placement (encode group_key), menu opens/closes, keyboard focusable.

### Task 6 — regression + visual acceptance + PR
- Full backend recon/resolution sweep + full FE vitest. Zero regressions.
- Rendered check: run FE against local backend, screenshot/eyeball `/reconciliation` (flag-ON tenant fixture) against the mock — layout, chip colors, right-aligned tabular amounts, copy affordance, export menus present. This gate, not tests, decides done.
- Final whole-branch review, then PR to main (squash-merge house style), push both remotes. Post-merge: backend auto-deploys; FE manual deploy MUST pass `NEXT_PUBLIC_BUILD_ID=$(git rev-parse --short HEAD)`.

## Out of scope (do not build)
- Workstream B payout-status refresh (separate plan, ticket 86baxk9xm).
- Sorting/filtering/pagination inside the worksheets (mock shows a "filter…" affordance — deferred; ship static sort by amount desc as today's API returns).
- Any change to approve/reject endpoints, planner, matcher, or evidence pack contents.
