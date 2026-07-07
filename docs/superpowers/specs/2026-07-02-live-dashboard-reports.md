# Live Dashboard Reports (Phase 6 of report quality) — Design Spec

**Status:** Product-decided 2026-07-06 (§6) — ready to implement, Slice A first. **Tier:** T2 (financial surface + alembic migration + cron/Beat + report tooling).
**Product direction (owner, 2026-07-02):** "these reports should be live dashboard. learn from claude."
**Depends on:** the report-quality stack (#155; #157→#158→#159→#160 — deterministic curation, statement treatment, chart selection, reporting skill). ClickUp: 86bapzbr9 (parent program).

---

## 1. Product intent

A report today is a frozen point-in-time HTML snapshot. The owner wants it to behave like a
**Claude artifact / live dashboard**: a stable URL you can keep open, numbers that stay
current, refresh on demand, and history.

## 2. The design translation — versioned auto-refresh, not view-time queries

**"Live" = the Claude-artifact model:** one stable URL; a refresh re-executes the report's
stored *recipe* and publishes the result as a **new immutable version at the same URL**;
prior versions remain viewable (version picker). Dashboard feel = auto-refresh schedule +
refresh button + "data as of" stamp.

**Explicitly rejected: re-querying at view time.** Three hard reasons:
1. **Auth fragility** — NetSuite OAuth refresh tokens are single-use; a view-time-query
   dashboard dies silently the way the nightly deposit sync did (see
   `reference_netsuite_oauth_single_use_token_and_recon_freshness_cursor`).
2. **Quota** — every page-load would hit tenant NetSuite/BigQuery.
3. **Auditability** — a financial statement must not change under an auditor's feet. A
   close packet pins a version id; versions are immutable rows.

## 3. What already exists (build on, don't rebuild)

- `reports` table already has `version`; `/api/v1/reports/{id}/view` is the stable URL.
- Every frozen `result_payload` already stores its originating read query
  (`query` field: SuiteQL text / `ns_runReport(reportId=…)` / metric id) — the recipe is
  half-captured today.
- The deterministic render pipeline (top-K curation, statement callouts+curated table,
  driver/trend chart selection, legible SVG) is the SAME code a refresh re-runs — zero
  new render logic.
- Beat/InstrumentedTask + `jobs` table for scheduling + failure history.

## 4. Slices (each its own PR + T2 gate)

### Slice A — Recipe capture (schema + compose)
- `reports.recipe_json` (alembic, T2): the ORIGINAL compose sections (pre-resolution) +
  per-result_id `{tool, params, connection_id}` captured at compose time from the turn's
  tool_calls. Additive column; no behavior change. Backfill: none (old reports stay
  snapshot-only; the UI shows Refresh only when a recipe exists).
- Trust boundary: the recipe is server-captured from executed tool calls — never
  model-authored post-hoc. Read-only tools ONLY may enter a recipe (suiteql validator
  read-only + ns_runReport + metric_compute allowlist); mutation tools never.

### Slice B — Manual refresh (the artifact "redeploy")
- `POST /api/v1/reports/{id}/refresh`: re-execute the recipe's reads (tenant-scoped via
  stored connection ids + `set_tenant_context`), rebuild payloads through
  `extract_result_payload` (so `line_meta`/currency tagging regenerate), re-run
  `assemble_spec` with the ORIGINAL sections, render, insert as version N+1. Audit event
  `report.refresh` with the acting user. Permission: report READ permission (§6 decision —
  it is a read-only replay); no HITL needed (reads only) but every refresh is audited AND
  per-report debounced (minimum interval between refreshes, proposal 5 min) so viewers
  cannot burn tenant NetSuite quota or churn versions.
- Narrative honesty: prose is compose-time text; `{{result:…}}` placeholders re-resolve
  against fresh payloads, but claims in prose can go stale → footer stamps
  "narrative composed <date>; data refreshed <date>". (Re-composing narrative via the
  agent is a LATER, opt-in slice — it re-enters LLM cost + benchmark territory.)
- FE: Refresh button + "data as of" stamp + version picker (list versions, view any).

### Slice C — Dashboard mode (auto-refresh)
- Per-report `auto_refresh` interval (off | hourly | daily; **default `daily`** for newly
  composed recipe-bearing reports, §6) — Beat `InstrumentedTask` sweep refreshes due
  reports (T2: cron). Failure = keep last good version + staleness banner ("data as of …;
  last refresh failed: reconnect NetSuite") — NEVER a broken page, NEVER auto-retry storms
  against a dead OAuth connection (single-use-token death is the known failure mode):
  hourly backs off to daily on repeated failure, and after N consecutive failed refreshes
  (proposal: 7) auto-refresh PAUSES for that report (banner explains; one-click resume
  after reconnect). With daily-by-default this pause ladder is launch-critical, not
  polish.
- Version retention: cap stored versions per report at **30** (§6), pinned versions
  exempt, the latest/current version never pruned; prune oldest-unpinned first.

### Slice D — "Learn from Claude" rendering polish
- Self-contained interactivity in the report HTML (no CDNs, CSP-safe, inline only):
  series toggle, hover values (SVG `<title>` exists), sticky header, print stylesheet.
  NOT React; the artifact must stay a single self-contained document.

## 5. Invariants (unchanged from the stack)
- **No-LLM-numbers:** refresh has NO model in the loop — tools → deterministic pipeline.
- **Deterministic-first:** curation/chart selection enforced in code (proven live).
- **No prompt pollution:** recipes are data, not prompt content.
- **HITL:** recipes are read-only by construction; mutation tools structurally excluded.
- **RLS/tenant scoping:** refresh runs under the report's tenant context; connections
  resolved per-tenant; a missing/errored connection degrades to staleness, never crosses.

## 6. Product decisions (owner, 2026-07-06)
1. **Default `auto_refresh` = `daily`** for newly composed recipe-bearing reports once
   Slice C ships (Slices A/B ship first with manual refresh only; legacy reports without
   a recipe stay snapshot-only). Consequence: the Slice-C staleness machinery (last-good
   version + banner + consecutive-failure pause, §4C) is launch-critical — daily-by-default
   multiplies the background NetSuite consumer surface, and the known single-use
   refresh-token death must degrade loudly-but-gracefully, never a silent retry storm.
2. **Version retention = 30** unpinned versions per report; pinned exempt; latest never
   pruned; prune oldest-unpinned first.
3. **Any viewer with report READ permission may refresh** — a read-only replay; every
   refresh audited with the actor; per-report debounce (proposal 5 min) guards quota and
   version churn.

## 7. Definition of done (program-level)
A Framework cash-flow report opened a week after composition shows a staleness stamp, a
working Refresh that produces a new version with current NetSuite numbers through the
full deterministic pipeline, a version picker that can show the original, and (dashboard
mode on) auto-refreshed numbers with a graceful staleness banner when the NetSuite
connection is dead — verified live + T2 gates per slice.
