# Publishable Report Renderer — Slice 1 Design Spec

**Date:** 2026-06-09
**Status:** Design approved (brainstorm), spec under review
**Feature branch:** `feat/publishable-report-renderer`
**Tier:** **T2** (new RLS table + Alembic migration + new chat prompt surface; Slice 2 adds an external Drive write)
**North-star:** Bet 2 — "stitch multisource data → publishable report" (see memory `project_three_north_stars_roadmap`). Builds on the shipped cross-source join engine (PR #108) + metric catalog (PR #124).

---

## 1. Goal

Let a user, from chat, generate a **branded, story-telling financial report** that stitches data already computed by our deterministic backend (cross-source joins, blessed metrics, financial reports, recon), then **click that report and have it render as a page in their browser** — beautiful, frozen, and accurate.

A report is a **point-in-time, self-contained artifact**: composed once, saved, and served on demand. The numbers never change after generation; the in-browser view is the frozen truth.

## 2. Non-goals (this slice)

- **PDF export + Google Drive publishing** → **Slice 2** (separate spec). Slice 1 ends at "the report renders in the browser."
- **Live/interactive dashboards** — a report is frozen, not a live query surface. No per-view recompute, no drill-down.
- **External/anonymous sharing** — audience is **internal** (authenticated tenant users). No public share tokens, no anonymous routes.
- **A general report-template designer / WYSIWYG editor** — the LLM composes the structure; humans regenerate, not hand-edit (a "regenerate" produces a new version).
- **New data sources** — we consume what the existing compute tools already produce (NetSuite SuiteQL, BigQuery, cross-source join, metrics, financial reports, recon). Stripe/Sheets-into-join remains out of scope (it's a cross_source limitation, not this feature's).

## 3. Architecture

Three planes; Slice 1 is Planes 1–2.

```
PLANE 1 — COMPOSE (chat-triggered, deterministic)
  unified agent runs data tools (cross_source.query / metric.compute /
  financial_report / recon) → each result is intercepted + cached (existing path)
     │
     ▼  agent calls report.compose(title, sections[]) referencing cached result_ids
  report_service:
     • resolves each result_id → the FULL frozen backend-computed payload
     • fills templated-narrative placeholders with frozen values
     • renders each chart section → static inline SVG (server-side, no browser)
     • assembles spec_json (canonical) + renders the self-contained HTML artifact
     • saves a reports row (RLS-scoped)
     • returns {report_id, title, url} — condensed for the LLM (NO numbers)

PLANE 2 — RENDER ON DEMAND (in-app, browserless)
  Reports list  +  /reports/[id] view
     │  click a report
     ▼
  serve the saved self-contained HTML (the exact frozen bytes) inside a thin
  neubrutalist app page (slim action bar). Rendered in an <iframe> for CSS
  isolation. Instant — no recompute, no data fetch.

PLANE 3 — PUBLISH (Slice 2, out of scope here)
  weasyprint converts the SAME saved HTML → PDF on demand → cache → upload to
  the tenant's Google Drive → audit-logged.
```

**Two load-bearing decisions, already made in brainstorm:**

1. **Frozen snapshot, not live-recompute.** The report stores the *computed values* at compose time (with metric `definition_version` provenance for footnotes). This makes the artifact reproducible, immutable, and guarantees in-browser-view == (future) PDF.
2. **Browserless rendering (Option A).** Charts are rendered to **static SVG server-side**; the report is a **self-contained HTML document**. No headless browser anywhere → no Chromium, no new container, no RAM/OOM risk on the 0-swap staging VM (the documented recurring failure mode). The price is a focused server-side SVG chart renderer — which neubrutalism (flat fills, hard edges, no gradients/animation) makes tractable, and which is *this report's own visual surface* (it need not pixel-match chat's recharts, so there is no "two-renderer drift" liability).

## 4. The trust boundary (non-negotiable)

Extends the existing no-LLM-numbers invariant cross-feature. **The LLM never types a number into a report.**

- The agent composes by **referencing cached result_ids**, never by passing data values. The backend holds the real frozen numbers (in the result cache) and resolves them.
- **Templated narrative:** a `narrative` section's prose may embed placeholders (`{{result:<id>.<field>}}` / `{{metric:<id>}}`). The LLM authors *sentence structure and qualitative framing*; the backend **substitutes the frozen computed value** at compose time. So prose tells a numeric story without the LLM emitting a figure.
- `report.compose`'s return value to the LLM is condensed (`{report_id, title, url, section_count}`) with a "do NOT restate the figures" note — same pattern as `data_table` / `metric` interception (`_intercept_tool_result`).

## 5. Data model

New table `reports` (Alembic migration `084_reports`; RLS parity with the `082` metric-definitions policy verified on staging).

| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid, FK tenants, NOT NULL | RLS: `USING (tenant_id = get_current_tenant_id())` **+ `WITH CHECK (tenant_id = get_current_tenant_id())`** (no SYSTEM branch — reports are never SYSTEM-owned) |
| `title` | text NOT NULL | |
| `spec_json` | jsonb NOT NULL | canonical structured record: ordered sections with **resolved frozen payloads** + provenance (see §6) |
| `rendered_html` | text NOT NULL | the self-contained HTML artifact — the exact bytes served in-browser **and** (Slice 2) fed to weasyprint. Produced by one `render_report_html(spec_json)` function so screen == PDF by construction |
| `status` | text NOT NULL default `'draft'` | `draft` \| `published` (`published` used in Slice 2) |
| `source_run_id` | uuid nullable | the chat run that composed it (traceability) |
| `created_by` | uuid FK users | |
| `version` | int NOT NULL default 1 | regenerate → new row with incremented version (immutable history) |
| `published_drive_url` | text nullable | Slice 2 |
| `published_at` | timestamptz nullable | Slice 2 |
| `created_at` / `updated_at` | timestamptz | |

- **RLS:** `FORCE ROW LEVEL SECURITY` (parity with metric_definitions 081/082, so the non-bypass app role is subject to it). Both read and write pinned to the caller's tenant.
- **Storage of `rendered_html`:** stored in-row (text). Reports are small (one document); no blob store needed in Slice 1.

## 6. The `spec_json` section contract

`spec_json = { title, generated_at, sections: Section[], provenance: {...} }`.

`Section` is a discriminated union on `type`. Data sections embed the **resolved frozen payload** (the canonical record stores values, not just a result_id, so the report is self-contained even after the result cache expires):

| `type` | fields | renders as |
|---|---|---|
| `heading` | `level (1-3)`, `text` | neubrutalist section header |
| `narrative` | `markdown` (post-substitution: placeholders already replaced with frozen values) | prose block |
| `metric_headline` | `label`, `value`, `unit`, `definition_version`, `period` | big stat card with a provenance footnote |
| `chart` | `chart_data` (existing `ChartData` schema), `svg` (the pre-rendered static SVG string) | the SVG, inline |
| `table` | `columns`, `rows`, `row_count`, `truncated`, `source_provenance` | neubrutalist data table |
| `divider` | — | rule |

`provenance` aggregates every contributing source (tool name, metric keys + `definition_version`, query identifiers, `generated_at`) for an end-of-report "Sources & definitions" footer — the auditability story.

## 7. The `report.compose` tool

Claims the **existing reserved `report.export` stub** (`registry.py:271`, currently `{"status":"stub"}`), renamed/repurposed to `report.compose` (keep the registry slot; update name + schema). Registration touch-points (all single-edit, per the context map):

- `registry.py` — `{description, execute, params_schema}`.
- `nodes.py` `ALLOWED_CHAT_TOOLS` — already lists the stub; update the dotted name.
- `tool_categories.py::_EXACT` — new category `report` (drives interception + source-pin).
- `tool_inventory.py` — auto-renders into `{{TOOL_INVENTORY}}` (no hardcoding; `test_prompt_tool_sync.py` enforces this).
- A `reporting.yaml` knowledge profile (trigger_tools = `report_compose`) injects compose guidance only when the tool is in play.

**Input:** `{ title, sections: ComposeSection[] }` where a `ComposeSection` references data by **`result_id`** (a prior cached tool result in the same turn), never by value:
- `{type:'heading', level, text}`
- `{type:'narrative', markdown}` (may contain `{{result:<id>.<field>}}` / `{{metric:<id>}}`)
- `{type:'metric_headline', result_id, label}`
- `{type:'chart', result_id, chart_type?, options?}`
- `{type:'table', result_id, select?}`
- `{type:'divider'}`

**`execute()` (in `report_service.compose_report`):**
1. For each data section, resolve `result_id` → the **full** frozen payload from the result cache (see §16 open question on full-vs-preview rows).
2. Substitute narrative placeholders with the resolved frozen values (string-safe; a missing/invalid ref → an explicit "[unresolved: <ref>]" marker, never a fabricated value).
3. For each `chart` section, render `chart_data` → static SVG via the new renderer (§8).
4. Assemble `spec_json` (frozen payloads embedded) + `provenance`.
5. `render_report_html(spec_json)` → the self-contained HTML artifact.
6. Persist a `reports` row (tenant-scoped, `set_tenant_context`), `status='draft'`.
7. Return condensed `{report_id, title, url, section_count}` to the LLM (no numbers). `_intercept_tool_result` emits a `report_ready` SSE event `{report_id, title, url}` for the chat card.

**Execution budget:** the data tools have already run (their cost is borne earlier in the turn); `report.compose` itself is assembly + SVG render + one insert — well within the 300s in-turn background-task budget. (Heavy *data* gathering is the agent's prior tool calls, unchanged by this feature.)

## 8. Server-side neubrutalist SVG chart renderer — the one genuinely net-new build

A pure-Python module `report_charts.py`: `render_chart_svg(chart_data: ChartData) -> str`.

- Input is the **existing** `ChartData` schema (`backend/app/schemas/chart.py`): 7 types — bar, line, pie, area, scatter, donut, histogram.
- Output is a self-contained `<svg>` string (fixed dimensions, inline styles) — no external deps, no JS, embeddable directly in the artifact.
- **Neubrutalist by design:** thick black strokes, flat high-contrast fills, hard offset shadows (rect `<filter>`-free, just an offset duplicate), bold labels, no gradients/animation. This is *simpler* than recharts output, not harder.
- Reuses the deterministic spec path already proven server-side (`financial_chart_builder.py` builds `ChartData` with no LLM/browser; this module draws it).
- Currency/number tick formatting mirrors the frontend `formatValue` semantics so figures read correctly.
- **No matplotlib/plotly** (heavy, off-brand visuals). A focused hand-rolled SVG emitter for these 7 shapes. Library choice (tiny SVG helper vs hand-rolled) is a planning decision; constraint: zero browser, neubrutalist output.

## 9. In-browser render (Plane 2)

- **Serve route (backend):** `GET /api/v1/reports/{id}/view` → `text/html` returning `reports.rendered_html`, tenant-scoped via `get_current_user` (the report's RLS ensures cross-tenant 404). `GET /api/v1/reports/{id}` → JSON metadata. `GET /api/v1/reports` → tenant's report list.
- **Frontend:** `app/(dashboard)/reports/page.tsx` (list) + `app/(dashboard)/reports/[id]/page.tsx` (view). The view page is a thin neubrutalist shell: a slim action bar (Back · *Publish to Drive [Slice 2, disabled]* · *Download PDF [Slice 2, disabled]*) wrapping the artifact in an **`<iframe src={/api/v1/reports/{id}/view}>`**.
  - **iframe rationale:** (1) the report's inline neubrutalism CSS is isolated from the app's Tailwind — no clashes; (2) it's the *same bytes* Slice 2's weasyprint consumes → guaranteed screen==PDF.
  - Auth: the iframe `src` is an authenticated API URL; the SPA already attaches the bearer via `apiClient`. (Confirm iframe credential handling in planning — see §16.)
- **Chat entry point:** a new `report_ready` `StreamBlock` (normalized in `chat-stream.ts`, rendered in `message-list.tsx` as a card modeled on `docs_link`/`sheets_link`) with title + "Open report" → navigates to `/reports/[id]`.

## 10. Neubrutalism design tokens

Defined once as a CSS token layer used by the artifact template (and, where it shows app chrome, the thin wrapper):

- **Surface:** subtle off-white background (e.g. `#FAF9F6`), pure-white cards.
- **Borders:** thick solid black (`3–4px`).
- **Shadows:** hard offset, no blur (e.g. `6px 6px 0 #000`).
- **Type:** bold, heavy weights; large headline scale; high contrast.
- **Accent:** blocky high-contrast panels; seeded from `TenantConfig.brand_color_hsl` so it's white-label aware (the accent, not the whole palette — neubrutalism stays high-contrast).
- **Fonts:** bundle the chosen heavy display font into the artifact via `@font-face` (no reliance on system fonts — matters for Slice 2 PDF fidelity).

## 11. Error handling (never fabricate)

- **Compose — a referenced result_id is missing/expired:** that section renders an explicit "data unavailable: <reason>" block; the rest of the report still composes. (Mirrors metric `ComputeError`.)
- **Compose — narrative placeholder unresolved:** inline `[unresolved: <ref>]` marker; never a guessed value.
- **Render — `rendered_html` missing/corrupt:** the view route returns a graceful neubrutalist error page, not a 500.
- **Cross-tenant access:** RLS → the row is invisible → `404` (not `403`, to avoid existence disclosure).

## 12. Testing (TDD strict — failing test first, every task)

- **Backend unit:**
  - `report_service.compose_report` assembles `spec_json` correctly from mocked cached payloads.
  - **Trust-boundary test:** the LLM-facing condensed string + the filled narrative contain **no un-computed numbers**; a `narrative` placeholder is replaced by the backend value, and the LLM cannot inject a raw figure.
  - **RLS test:** cross-tenant `reports` read/write rejected under a non-bypass role (parity with the 082 smoke we just ran).
  - Error paths: missing result_id → error block; unresolved placeholder → marker.
- **Chart renderer:** each of the 7 `ChartData` types → a valid, non-empty `<svg>`; deterministic output (same input → same SVG); currency formatting correct.
- **Render route:** returns the stored HTML with `text/html`; tenant-scoped 404 on cross-tenant id.
- **Frontend (vitest):** list + view components; the `report_ready` block normalizes + renders; neubrutalism token snapshot.
- **Playwright e2e (gated in CI, per the frontend-e2e rule):** golden path — compose a report in chat → "Open report" card → `/reports/[id]` renders the artifact (assert a known heading + a chart `<svg>` present) → edge cases (missing-data section renders an error block, not a crash).

## 13. Tier & review gates (T2)

- **CI:** existing + **seeded-tenant e2e** + the Playwright golden path (gating, not `continue-on-error`).
- **Pre-merge, blocking:** `code-review-multiangle` (Claude 7-angle, fails closed) **AND** **grill-me / codex** (independent-model cross-examination — catches Claude's blind spots; needs `dangerouslyDisableSandbox`). Run **both**.
- **Post-deploy:** safe-envelope live smoke (compose + view a report against the `uat-smoke` staging tenant; zero-residue cleanup by report id).
- Self-review does **not** substitute for either gate.

## 14. Slice 2 preview (out of scope — separate spec)

Publish-to-Google: `weasyprint` converts the saved `rendered_html` → PDF (charts already SVG, no JS) → cache the PDF → upload to the tenant's Drive via the existing service-account `files().create` pattern (`docs_service.py`) → `audit_service.log_event(category='export')` → flip `status='published'`, set `published_drive_url`. Lightweight: weasyprint adds ~3 apt libs (cairo/pango/gdk-pixbuf) to the slim base; **no browser, no new container**. The "Publish to Drive" / "Download PDF" action-bar buttons activate.

## 15. Components summary

**New (backend):** `models/report.py`; migration `084_reports` (+ RLS, FORCE RLS); `services/report_service.py` (compose + `render_report_html`); `services/report_charts.py` (SVG renderer); `mcp/tools/report_tool.py` (repurpose the `report.export` stub → `report.compose`); `api/v1/reports.py` (list/get/view); `knowledge_profiles/reporting.yaml`; a `report_ready` branch in `_intercept_tool_result`.
**New (frontend):** `app/(dashboard)/reports/page.tsx` + `[id]/page.tsx`; a `report_ready` `StreamBlock` + card; neubrutalism token CSS.
**Reused (do not reimplement):** cross_source / metric / financial / recon compute; result cache + `reference_previous_result`; `_intercept_tool_result`; `TenantConfig` branding; `audit_service`; `set_tenant_context` / RLS idiom; the `ChartData` schema.

## 16. Open questions (resolve in planning/research)

1. **Full-rows vs preview in the result cache.** `report.compose` must embed the **full** frozen table (not the LLM's 50-row preview). Verify what `_build_intercept_cache_entry` / `CachedResult` persists; if it stores only the preview, add a report-scoped full-payload store or raise the cap for report-bound results. *(Load-bearing — the table sections depend on it.)*
2. **iframe auth.** The `<iframe src=/api/v1/reports/{id}/view>` must carry the bearer. Options: a same-origin authenticated fetch → blob URL, or a short-lived view token. Decide in planning (keep it internal-only, no anonymous access).
3. **Compose ergonomics.** Does the agent reliably reference prior `result_id`s, or do we need a lighter "compose from the last N results" affordance? Validate against the vs-MCP benchmark (per the benchmark invariant).
4. **SVG chart renderer scope.** Confirm all 7 `ChartData` types are actually needed for v1, or start with the 3–4 that financial reports emit (bar/line/pie/area) and defer the rest. YAGNI.
5. **Regenerate semantics.** New version row vs in-place — pin the versioning UX.
