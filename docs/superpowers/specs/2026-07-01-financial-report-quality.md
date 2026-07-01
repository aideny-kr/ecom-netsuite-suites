# Implementation Doc: Financial Report Quality Redesign

**Status:** Ready for implementation. **Owner:** next agent. **Tier:** T2 (financial surface + prompt-pollution surface — full gate + seeded e2e + live smoke).
**Context:** PR #150 (alias reliability) + #151 (deterministic first-K curation + auto-chart) are merged & live on staging. #151 stopped the 866-row dump but produced a **bad report** — see symptoms. This doc is the fix.

---

## 1. Symptoms (observed on live Framework report `9451e03d`, screenshots reviewed)

1. **Illegible charts.** X-axis labels overlap into an unreadable black smear. The SVG bar renderer stamps a `<text>` under every bar with `text-anchor="middle"` and **no rotation, no truncation, no category cap**, so 12–36 long labels ("11010 - Intercompany Receivables", "10005 - SVB Sweep Account -6603 (inactive)") collide.
2. **Meaningless charts.**
   - "amount by account" bars mix a placeholder ("Financial Row"), **section subtotals** ("Operating Activities"), AND **their own detail lines** ("11000 - Accounts Receivable") side by side → double-counting (Net Income is *inside* Operating Activities).
   - "Cash Balance Trend" section charts **~36 individual bank accounts** including a **"Total Cash" grand-total bar** that dwarfs everything — instead of the monthly trend the section title promises.
3. **Table is a raw statement slice.** "First 12 of 180 rows" = a "Financial Row" placeholder + section headers + subtotals + detail GL lines + **two blank-label rows**. Not a summary.
4. **Junk chart titles** — "Chart", "amount by account" (auto-generated).
5. **Wrong chart type for time-series** — cash-balance-over-months should be a **line**, not account bars.

**Net:** the report is short but unreadable and non-informative. Product owner: *"this is the worst looking report."*

---

## 2. Target (product intent, stated repeatedly)

A financial report = **a few meaningful named figures + legible, meaningful chart(s) + the risk narrative.** NOT raw statement rows, NOT bar-soup.

Concretely, for a cash-flow/statement report:
- **Key figures** (named): e.g. Net Income, Operating Cash Flow, Net Change in Cash, Ending Cash — as clean callouts (`metric_headline`) or a **short curated statement of the section subtotals** (≤~8 lines), each with a real label. No blanks, no placeholder, no mixing subtotal+detail.
- **Meaningful chart(s), legible:**
  - time-series (cash balance by month) → **line chart over the periods**;
  - drivers (AR, Inventory, Intercompany) → **bar of the top-K drivers by magnitude, EXCLUDING grand-totals/subtotals-with-their-own-details**, with readable labels + a descriptive title.
- **Risk narrative** — already good, keep.

The narrative already reads well; the failure is entirely in **what data gets tabled/charted and how the chart renders.**

---

## 3. Root causes → code map

### RC-1 — reportData flatten discards all hierarchy (the core cause)
`backend/app/services/chat/tool_call_results.py`
- `_extract_report_data_as_table()` (L206) and `report_data_to_capped_table()` (L258) flatten `ns_runReport` reportData to **`columns=["account","amount"]`**, one row per entry.
- It **intentionally drops the line-type marker** (L210-213 comment) and **keeps blank-label continuation rows** (L242-252), explicitly saying: *"Cosmetic blank-label continuation rows … left for the Tier-2 curated-statement restructure, which has the structure to consolidate safely."* → **that restructure is THIS work.**
- **Available-but-discarded structure:** each reportData entry has `summaryLineValues` vs `detailLineValues` (L227) — this distinguishes **section/subtotal lines from detail lines**. There is also typically an indent/level and a line-type in the raw entry. USE this to build a meaningful curated statement instead of a flat dump.

### RC-2 — first-K curation on a flat hierarchical dump is meaningless
`backend/app/services/report/report_service.py`
- `_curate_table_rows(rows, k)` (~L100, `_REPORT_TABLE_TOP_K=12`) takes the FIRST 12 rows. On a hierarchical statement that's a placeholder + sections + subtotals + details + blanks. First-K was a deliberate choice (magnitude-ranking was worse — it scrambled statement order, T2-gate finding), but neither is a *summary*. **The summary must come from structure (RC-1), not row position.**

### RC-3 — auto-chart charts the raw (mixed-hierarchy) table + grand totals
`backend/app/services/report/report_service.py`
- `_auto_chart_section()` + `_build_tabular_chart()` (~L114-190) chart the resolved table's rows as bars. Since the table is a mixed dump, the chart mixes subtotals+details+grand-totals. Grand-total/subtotal rows must be **excluded** from a driver chart (or the chart is double-counted + one bar dominates).
- Cash-balance chart plots ~36 accounts because the model composed/auto-charted an account-level result rather than the monthly-trend result; and the chart resolver caps at `_MAX_CHART_POINTS=100`, not the table's 12.

### RC-4 — SVG renderer can't render labels legibly
`backend/app/services/report/report_charts.py`
- `_bars()` (~L67): x-axis label per bar at `y=bottom+20`, `text-anchor="middle"`, **no rotation / truncation / max-category limit**. `_W=720`. Long labels + many categories = overlap smear.
- `_lines()` same. No descriptive default title (`_build_tabular_chart` title default = `"{numeric_cols[0]} by {cols[0]}"`; explicit charts default to `"Chart"`).

---

## 4. Implementation plan (phased; each phase independently shippable)

> TDD throughout. Each phase is its own PR + T2 gate. Prompt-first was proven insufficient live — prefer **deterministic** transforms; use prompt guidance only as a secondary nudge. Respect the no-LLM-numbers invariant (numbers flow from tool payloads via SSE interception, never model text) and no-prompt-pollution (no hardcoded account/column names in prompts; code-side structural logic keyed off reportData markers is fine).

### Phase 1 — Chart legibility (renderer). *Fastest visible win; do first.*
File: `report_charts.py` (`_bars`, `_lines`, shared).
- **Rotate or truncate x-labels.** For >~6 categories or any label >~10 chars: rotate labels ~30–45° (`transform="rotate(...)"`) OR ellipsis-truncate to N chars with a `<title>` tooltip. Increase bottom padding when rotating.
- **Cap categories for bar charts** (e.g. ≤ ~12) — if more, the caller should have aggregated; render a legible subset + note, never a smear.
- **Descriptive titles** — thread a real title through (see Phase 4); stop defaulting to "Chart".
- Tests: assert labels don't overlap (spacing/rotation attr present), long labels truncated, category cap enforced. (These are the guards the last gate said were missing — assert on structure, not SVG substrings.)

### Phase 2 — Structure-preserving reportData flatten.
File: `tool_call_results.py` (`_extract_report_data_as_table` / `report_data_to_capped_table`).
- Emit hierarchy metadata per row: `is_summary` (has `summaryLineValues`) vs detail, an indent/level, and drop the **blank-label continuation rows** (they duplicate the prior amount). Keep `[account, amount]` as the rendered columns but carry the level/type as **sidecar metadata** (NOT as a visible first column — that was the original concern) so the resolver can curate by it.
- Preserve the true total for the disclosure; keep the currency tagging (`currency_columns`) intact.
- Tests: a known reportData fixture → summary lines flagged, blanks dropped, placeholder handled, amounts (incl. $0) preserved.

### Phase 3 — Curated statement + key figures (composition).
Files: `report_service.py` (resolver/assemble_spec), `report.compose` schema/profile.
- For a statement-shaped result, render **the summary/section lines** (a curated statement of ≤~8 named subtotals), OR promote the top figures to `metric_headline` callouts. Drop detail lines + blanks + the placeholder from the *table*.
- Decide table-vs-headlines: headlines for the 3–5 marquee numbers, a compact curated statement table for the section breakdown. Confirm the exact shape with the product owner.
- Tests: hierarchical fixture → table has named summary lines only, no blanks/placeholder, no detail+subtotal duplication.

### Phase 4 — Meaningful chart selection.
File: `report_service.py` (`_auto_chart_section` / chart selection).
- **Exclude grand-total & subtotal rows** (and their duplicated detail) from driver charts — chart only comparable leaf drivers, top-K by magnitude, with a descriptive title (e.g. "Top cash-flow drivers").
- **Pick chart type by data shape:** a period/time column (month/quarter/date) → **line**; categorical drivers → **bar**. The cash-balance-by-month result should render as a line over the 6 periods, not 36 account bars.
- Tests: time-series fixture → line; driver fixture → bar excluding totals; grand-total row never charted.

---

## 5. Constraints & invariants (do not regress)
- **No-LLM-numbers:** numbers come from tool payloads via `_intercept_tool_result` → SSE, never model prose. `fill_placeholders` uses `{{result:…}}`/`{{metric:…}}`.
- **No prompt pollution:** do NOT hardcode account/column names in `reporting.yaml`/prompts. Structural logic in code keyed off reportData markers is fine.
- **Currency formatting** (PR #145/#147): producer-tagged `currency_columns`, exact `Decimal`, accounting negatives — keep.
- **Deterministic-first:** prompt guidance alone does NOT reliably curate/chart (proven live twice). Enforce in code.
- **Chart safety already in place:** negative/zero-baseline handling, non-finite guard, `_MAX_CHART_POINTS`, pie full-circle — keep (see `report_charts.py`).
- **Frozen reports:** changes affect *future* compositions only; regenerate to verify.
- **TDD + T2 gate** (`Workflow code-review-multiangle`) + seeded e2e (`tests/e2e/test_report_lifecycle_e2e.py`) + live-verify on Framework. NOTE: the gate's verifier stage has been intermittently rate-limited — self-verify findings if so.

## 6. Separate, related issues (own tickets — NOT this doc's scope)
- **`/cashflow` 300s timeout** = LLM per-turn latency (~2-min extended-thinking turns × several turns), amplified by the Sonnet-5 + adaptive-thinking migration (PR #152). Levers: tune thinking budget for report/tool-orchestration turns (`thinking.py`), raise `_BACKGROUND_TASK_TIMEOUT` (`chat.py:397`), reduce turns. **Already handed to another agent.**
- **`netsuite_financial_report type=balance_sheet_trend` → NetSuite 400 "Invalid search query"** — invalid SuiteQL the tool generates; wastes agent turns.

## 7. Definition of done
A regenerated Framework cash-flow report shows: a few **named** key figures (or a ≤~8-line curated statement, no blanks/placeholder), **legible** chart(s) with readable labels + descriptive titles, a **line** for the monthly cash trend, **no** grand-total/subtotal bar-soup, and the risk narrative — verified live on Framework + by the T2 gate.
