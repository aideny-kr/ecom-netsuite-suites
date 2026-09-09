from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from html import escape

from app.services.report.inventory_aging import (
    AgingReport,
    Provenance,
    share_pct,
)

# A string we will coerce to a currency amount: optional sign, US thousands-grouping
# (1,234,567) OR a plain integer part (no leading zeros — "0042" is a code, not $42),
# optional decimals, optional scientific exponent. Deliberately STRICT — it must NOT
# match locale-formatted ("1.234,56"), mis-grouped ("1,2,3"), underscore-separated
# ("1_000"), zero-padded ("0042"), or sentinel ("inf"/"nan") strings, which would
# otherwise be mangled into a wrong (or blank) dollar figure.
_AMOUNT_STR_RE = re.compile(r"^[+-]?([1-9]\d{0,2}(,\d{3})+|0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")

_CSS = """
:root { --bg:#FAF9F6; --ink:#111; --border:#000; --card:#FFF; --accent:hsl(%(accent)s); --accent-ink:%(accent_ink)s; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:'Inter',system-ui,-apple-system,sans-serif; line-height:1.5; }
.report { max-width:840px; margin:0 auto; padding:48px 32px; }
h1,h2,h3 { font-weight:800; letter-spacing:-0.02em; margin:1.4em 0 0.4em; }
h1 { font-size:38px; } h2 { font-size:26px; } h3 { font-size:20px; }
.nb-card { background:var(--card); border:3px solid var(--border); box-shadow:6px 6px 0 var(--border);
  padding:24px; margin:24px 0; }
.metric { display:flex; flex-direction:column; gap:4px; }
.metric .value { font-size:44px; font-weight:800; }
.metric .label { font-size:14px; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
.metric .foot { font-size:12px; color:#666; }
.accent-bar { height:10px; background:var(--accent); border:3px solid var(--border); margin:0 0 24px; }
table { width:100%%; border-collapse:collapse; }
th,td { border:2px solid var(--border); padding:8px 12px; text-align:left; font-size:14px; }
th { background:var(--accent); font-weight:800; color:var(--accent-ink); }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.divider { height:0; border-top:3px solid var(--border); margin:32px 0; }
.svg-wrap { overflow:auto; }
.prov { font-size:12px; color:#666; border-top:2px dashed #999; margin-top:48px; padding-top:12px; }
.stamp { font-size:12px; color:#666; margin-top:32px; }
/* Slice D — sticky table headers. The overflow-x wrapper forces computed overflow-y,
   so a document-relative sticky thead can never engage; the table card is instead a
   capped-height scroll region and the thead sticks to ITS scroll box. Short tables
   are unaffected (max-height only caps). th's opaque accent background keeps rows
   from bleeding through; the inset shadow re-draws the border that border-collapse
   detaches from a stuck header. */
.table-wrap { max-height:70vh; overflow-y:auto; }
.table-wrap thead th { position:sticky; top:0; z-index:1; box-shadow:inset 0 -2px 0 var(--border); }
/* Slice D — chart legend (emitted by report_charts after each multi-series svg). */
.chart-legend { display:flex; flex-wrap:wrap; gap:8px 16px; margin-top:12px; font-size:13px; font-weight:600; }
.chart-legend label { display:inline-flex; align-items:center; gap:6px; cursor:pointer; }
.chart-legend .swatch { width:12px; height:12px; border:2px solid var(--border); display:inline-block; }
/* Slice D — CSS-only series toggles: unchecking the legend's ser-j checkbox hides
   that series' <g class="ser-j"> groups. :has() is load-bearing (CSS has no parent
   combinator; id/for label pairs would collide across a report's charts); browsers
   without :has() degrade to inert checkboxes. Plain literals, NOT generated — and
   note this whole string passes through percent-formatting, so a percent sign in
   ANY rule or comment here must be doubled (this comment learned that first-hand).
   Rules exist for ser-0..ser-11 = report_charts._MAX_TOGGLE_SERIES — the legend
   stops emitting checkboxes past that cap (a rule-less checkbox is a dead control)
   and a drift test binds the two. */
.nb-card:has(input.ser-0:not(:checked)) svg .ser-0 { display:none; }
.nb-card:has(input.ser-1:not(:checked)) svg .ser-1 { display:none; }
.nb-card:has(input.ser-2:not(:checked)) svg .ser-2 { display:none; }
.nb-card:has(input.ser-3:not(:checked)) svg .ser-3 { display:none; }
.nb-card:has(input.ser-4:not(:checked)) svg .ser-4 { display:none; }
.nb-card:has(input.ser-5:not(:checked)) svg .ser-5 { display:none; }
.nb-card:has(input.ser-6:not(:checked)) svg .ser-6 { display:none; }
.nb-card:has(input.ser-7:not(:checked)) svg .ser-7 { display:none; }
.nb-card:has(input.ser-8:not(:checked)) svg .ser-8 { display:none; }
.nb-card:has(input.ser-9:not(:checked)) svg .ser-9 { display:none; }
.nb-card:has(input.ser-10:not(:checked)) svg .ser-10 { display:none; }
.nb-card:has(input.ser-11:not(:checked)) svg .ser-11 { display:none; }
/* Slice D — print. Un-clip the scroll regions (a stuck thead prints frozen mid-page
   and overflow-y clips rows off the paper), keep card/accent colors where the engine
   honors print-color-adjust (borders + weight-800 text stay legible where it strips
   them), hide the legend checkbox WIDGETS but keep swatch+label — the printed page
   shows exactly the series toggled on (WYSIWYG). Long tables paginate; the browser
   repeats <thead> per page natively. */
@media print {
  body { background:#fff; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
  .nb-card { box-shadow:none; break-inside:avoid; page-break-inside:avoid; }
  .svg-wrap, .table-wrap { overflow:visible; max-height:none; }
  .table-wrap { break-inside:auto; page-break-inside:auto; }
  /* engines that IGNORE print-color-adjust strip backgrounds — a computed light
     --accent-ink would then print white-on-white; pin light header + dark ink so
     printed headers are legible on every engine */
  thead th { position:static; background:#eee; color:var(--ink); }
  .chart-legend input { display:none; }
  .report { max-width:100%%; padding:0; }
}
"""

# Task 3 — the `financial_statement` section's CSS. This is a SEPARATE constant from
# `_CSS`, concatenated in via plain string `+` (NEVER passed through the `%` operator),
# for one reason: byte-stability. `_CSS` is %-formatted on EVERY render, so appending
# rules directly to it would change the <style> block (and therefore the exact rendered
# bytes) for every report, including ones with no financial_statement section at all —
# making the brief's "specs without a financial_statement section render byte-identically
# to today" requirement impossible to satisfy. Kept out of the %-format pipeline entirely,
# `_FS_CSS` needs NO %% doubling (see render_report_html: appended only when a
# financial_statement section is actually present) — the several literal `%` characters
# below (both in CSS values like `max-width:100%` and in comments) are safe verbatim
# either way.
#
# Reuses the base stylesheet's vars/idioms (--accent, --border, .nb-card box) and the
# generic table/th/td + td.num,th.num rules (money cells just get class="num" like every
# other table in this renderer) — only the differentiators called out in the design rule
# get bespoke classes here: KPI cards, chips/dots, quad+statement row emphasis, the
# CSS-only section-collapse, the trend chart legend, and print.
_FS_CSS = """
:root { --fs-good:#0A7A3D; --fs-bad:#B3261E; --fs-warn:#E8A13C; --fs-soft:#EFEDE7; }
/* Base tone rules — .fs-good/.fs-bad are applied DIRECTLY as a <td> class on statement/
   quad delta cells (_fs_delta_tone) and KPI deltas (_fs_sign_tone), not only alongside
   .fs-chip/.fs-dot/.fs-delta below. Those are scoped COMPANION rules for their own
   compound selectors; without this unscoped base rule a bare `class="num fs-good"` cell
   matches nothing and renders in the default ink color — exactly the bug a live review
   caught (statement + quad Δ$/Δ% cells rendering colorless). A drift test binds this. */
.fs-good { color:var(--fs-good); }
.fs-bad { color:var(--fs-bad); }
.fs-meta { display:flex; gap:8px; flex-wrap:wrap; margin:4px 0 14px; }
.fs-chip { font-size:11px; font-weight:700; border:2px solid var(--border); padding:3px 8px; background:var(--card); }
.fs-chip.fs-dark { background:var(--accent); color:var(--accent-ink); }
.fs-chip.fs-good { border-color:var(--fs-good); color:var(--fs-good); }
.fs-chip.fs-bad { border-color:var(--fs-bad); color:var(--fs-bad); }

.fs-watch { display:flex; gap:10px; flex-wrap:wrap; margin:12px 0 22px; }
.fs-witem { display:flex; align-items:center; gap:8px; border:2px solid var(--border);
  background:var(--card); padding:7px 10px; font-size:12.5px; font-weight:600; }
.fs-dot { width:9px; height:9px; border:2px solid var(--border); display:inline-block; flex:none; }
.fs-dot.fs-bad { background:var(--fs-bad); }
.fs-dot.fs-warn { background:var(--fs-warn); }
.fs-dot.fs-good { background:var(--fs-good); }

/* KPI card grid — label -> large value -> MoM/YoY delta -> sparkline (design rule #4). */
.fs-kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin:6px 0 22px; }
.fs-kpi { padding:14px 16px 12px; margin:0; position:relative; }
.fs-kpi-lbl { font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:#666; }
.fs-kpi-val { font-size:27px; font-weight:800; letter-spacing:-0.02em; margin-top:3px; }
.fs-kpi-sub { font-size:11.5px; color:#666; margin-top:1px; }
.fs-delta { font-size:12px; font-weight:700; }
.fs-delta.fs-good { color:var(--fs-good); }
.fs-delta.fs-bad { color:var(--fs-bad); }
.fs-spark { position:absolute; right:12px; bottom:10px; opacity:.9; }

/* EYEBALL-GATE FIX (F1, round 1): the trend card is emitted FIRST (see
   _financial_statement_html) so it should already own the wider track -- but every
   .fs-quad cell carries white-space:nowrap (the generic td.num,th.num rule), making the
   quad table's min-content width (~587px unwrapped, at the base stylesheet's 8px/12px
   cell padding) exceed its "fair share" of a plain fr split. A plain `fr` track's
   automatic minimum size defaults to its item's content size unless overridden, so the
   un-shrinkable quad ate space FROM the trend track regardless of the declared ratio --
   trend rendered in an unreadable ~170px sliver. minmax(0, Nfr) overrides that automatic
   per-item minimum (the grid-native equivalent of min-width:0), so the tracks actually
   honor their weights.
   EYEBALL-GATE FIX (F1, round 2): a 3:1 ratio cleared the trend floor but over-shrank
   the quad (~190px) -- its own "the four-column read" showed ZERO columns at rest.
   Tightening the quad's typography to the mock's scale (6px/8px cell padding) helped
   but wasn't sufficient on its own: at .report's ORIGINAL 840px max-width, the two
   floors (trend >=520px, quad's own unwrapped content ~443px + card padding) sum to
   more than the 758px .fs-mid actually has to split -- no ratio or reasonable
   typography closes a structural ~250px deficit (confirmed empirically, not guessed).
   EYEBALL-GATE FIX (F1, round 3): the real fix is canvas width, not the ratio. The
   approved mock's own canvas is 1060px, not 840px -- .report--wide (below) raises the
   ceiling to the DISPATCH-STATED 1120px cap for statement pages ONLY (never the shared
   .report default, which stays 840px for byte-stability on every other report type).
   The mock's OWN 1.5:1 ratio (tried first, empirically) still left the quad ~82px
   short of its own unwrapped content width even at this wider canvas -- solved
   algebraically from two measured constants (this .fs-mid's total avail width and the
   quad table's own natural width, both empirical, not estimated) for the ratio window
   that clears BOTH floors simultaneously: trend >= 520px AND quad-card >= quad's own
   ~483px need (443px table + 2*20px card padding). 1.1:1 sits solidly inside that
   window with margin on both sides. .fs-scroll stays as the safety net, not the
   primary mechanism, now that both the ceiling and the ratio are sized correctly. */
.report--wide { max-width:1120px; }
@media print { .report--wide { max-width:100%; } }
.fs-mid { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1fr); gap:18px; margin-bottom:22px; }
/* minor[6]: BS/TB have only a quad, no trend chart -- span it, don't leave a dead column */
.fs-mid--single { grid-template-columns:1fr; }
@media (max-width:900px) { .fs-mid { grid-template-columns:1fr; } }
.fs-scroll { overflow-x:auto; }
.fs-legend { display:flex; gap:16px; flex-wrap:wrap; font-size:12px; font-weight:600; margin-top:8px; }
.fs-sw { width:12px; height:12px; border:2px solid var(--border); display:inline-block;
  margin-right:5px; vertical-align:-1px; }

/* Variance quad (design rule #5): Actual | Prior | Delta $ | Delta % — reuses the same
   fs-sub/fs-formula/fs-net emphasis classes as the statement table below (both are built
   from the same _quad_row-shaped model dict). Cell padding tightened to the mock's own
   scale (6px 8px, vs the base stylesheet's 8px 12px); the label column is deliberately
   NOT forced nowrap (only the numeric/.num cells are, via the pre-existing generic
   td.num,th.num rule) -- a two-word metric label wrapping onto 2 lines is normal in a
   compact card and narrows the table's overall min-content width meaningfully more
   than tighter padding alone. .fs-mid > .nb-card gets its own tighter card padding too
   (below) -- both are what actually let the card fit its own "four-column read"
   unscrolled at .report--wide's width. */
.fs-mid > .nb-card { padding:18px 20px; }
table.fs-quad th, table.fs-quad td { border:none; border-bottom:1px solid #ddd; font-size:12.5px; padding:6px 8px; }
table.fs-quad th { text-transform:uppercase; font-size:10.5px; letter-spacing:.06em; color:#666;
  background:transparent; border-bottom:2px solid var(--border); }
table.fs-quad tr:last-child td { border-bottom:none; border-top:2px solid var(--border); font-weight:800; }

/* Statement table (design rule #6): section headers -> indented account detail (muted
   acct number) -> bold subtotals -> formula rows -> emphasized net row. Clean GAAP look
   (no per-cell grid) overriding the generic boxed th/td rules, scoped to .fs-stmt only. */
table.fs-stmt th, table.fs-stmt td { border:none; border-bottom:1px solid #ddd; }
table.fs-stmt thead th { background:var(--accent); color:var(--accent-ink); border-bottom:2px solid var(--border); }
table.fs-stmt tr.fs-sec td { background:var(--fs-soft); border-top:2px solid var(--border); border-bottom:none;
  font-weight:800; font-size:12px; letter-spacing:.05em; text-transform:uppercase; }
table.fs-stmt tr.fs-sec label.fs-sec-lbl { cursor:pointer; display:inline-flex; align-items:center;
  gap:6px; user-select:none; }
table.fs-stmt tr.fs-sec .car { display:inline-block; width:12px; transition:transform .12s; }
@media (prefers-reduced-motion:reduce) { table.fs-stmt tr.fs-sec .car { transition:none; } }
table.fs-stmt tr.fs-sec:has(input:not(:checked)) .car { transform:rotate(-90deg); }
table.fs-stmt tr.fs-acct td:first-child { padding-left:26px; font-weight:400; }
table.fs-stmt .fs-acct-no { color:#666; font-size:11px; margin-right:7px; }
table.fs-stmt tr.fs-acct:hover td { background:#f4f2ec; }
table.fs-stmt .fs-pct { color:#666; font-size:12px; }
table.fs-stmt tr.fs-sub td, table.fs-quad tr.fs-sub td { font-weight:700;
  border-top:2px solid var(--border); background:#fff; }
table.fs-stmt tr.fs-formula td, table.fs-quad tr.fs-formula td { font-weight:800;
  background:var(--fs-soft); border-top:3px solid var(--border); }
table.fs-stmt tr.fs-net td, table.fs-quad tr.fs-net td { font-weight:800; font-size:14.5px; background:var(--accent);
  color:var(--accent-ink); border-top:3px solid var(--border); }
tr.fs-check td { font-style:italic; font-size:12.5px; padding:8px 10px; border-top:2px solid var(--border); }
tr.fs-check.fs-good td { color:var(--fs-good); }
tr.fs-check.fs-bad td { color:var(--fs-bad); }

/* CSS-only collapse (design rule #14): a hidden-in-plain-sight checkbox wrapped by its
   own <label> (no id/for — several statement tables per report would collide) toggles
   that section's account rows via :has(); the subtotal row (no fs-of-N class) always
   stays visible even when collapsed. Rules exist for fs-sec-0..fs-sec-{cap-1} =
   _MAX_STATEMENT_SECTIONS (income_statement's 5 sections, the most any current
   statement type produces) — a section past the cap degrades to a non-interactive
   (always-expanded) header, same pattern as report_charts' ser-j legend cap. A drift
   test binds the two. */
table.fs-stmt:has(input.fs-sec-0:not(:checked)) tr.fs-of-0 { display:none; }
table.fs-stmt:has(input.fs-sec-1:not(:checked)) tr.fs-of-1 { display:none; }
table.fs-stmt:has(input.fs-sec-2:not(:checked)) tr.fs-of-2 { display:none; }
table.fs-stmt:has(input.fs-sec-3:not(:checked)) tr.fs-of-3 { display:none; }
table.fs-stmt:has(input.fs-sec-4:not(:checked)) tr.fs-of-4 { display:none; }

.fs-twocol { display:grid; grid-template-columns:1fr 1fr; gap:18px; margin:22px 0; }
@media (max-width:900px) { .fs-twocol { grid-template-columns:1fr; } }
.fs-hl { margin:0; padding-left:18px; }
.fs-hl li { font-size:13px; margin-bottom:9px; line-height:1.5; }
.fs-narr p { font-size:13.5px; line-height:1.65; margin:0 0 10px; }

/* Print (design rule #15): a financial statement prints FULLY EXPANDED regardless of
   on-screen collapse state — !important beats the :has() display:none rules above,
   which have no media qualifier and would otherwise still apply while printing whatever
   the on-screen checked state happened to be. Checkbox + chevron affordance (nothing to
   click on paper) is hidden; card colors already print via the base stylesheet's
   print-color-adjust:exact. */
@media print {
  table.fs-stmt tr.fs-acct { display:table-row !important; }
  table.fs-stmt input.fs-sec-cb, table.fs-stmt .car { display:none; }
  .fs-scroll { overflow:visible; }
}
"""

# Task 2 (Slice 1) — the inventory_aging section types' CSS. A SEPARATE constant,
# concatenated in via plain string `+` exactly like `_FS_CSS` above (never through the
# `%`-formatted `_CSS` pipeline) — same reason: byte-stability for every report that
# carries no inventory_aging section, and no %% doubling burden for the several literal
# `%` values below (`width:100%`, `height:34px` etc. — none needs escaping here).
# Namespaced `.ia-*`/scoped-to-`.chart`/`.tblcard` throughout so nothing here collides
# with `_CSS`'s generic `table`/`th`/`td` rules or `_FS_CSS`'s `.fs-*` ones — the mock's
# own paper/cream aesthetic is a deliberately different design language from the
# generic "notebook" stylesheet, so it gets its own color tokens rather than reusing
# `_FS_CSS`'s `--fs-good`/`--fs-bad` (which would be undefined on a report with
# inventory_aging sections but no financial_statement one).
_IA_CSS = """
:root {
  --ia-fav: #1B7A3E; --ia-unf: #B4232C; --ia-warn: #9A6400;
  --ia-line-soft: #D9D2C3; --ia-muted: #8E8B84; --ia-paper-2: #F4F1E9;
  --ia-b1: #E7E1D3; --ia-b2: #CFC7B4; --ia-b3: #AFA48C; --ia-b4: #6E6552; --ia-b5: #2B2722;
}
.ia-head { display: grid; grid-template-columns: 1fr auto; gap: 6px 24px; align-items: end;
  margin: -8px 0 22px; padding-bottom: 14px; border-bottom: 3px solid var(--border); }
.ia-sub { color: var(--ia-muted); font-size: 12.5px; }
.ia-meta { text-align: right; font-size: 11.5px; color: var(--ia-muted); line-height: 1.6; }
.ia-meta b { color: var(--ink); font-weight: 600; }
@media (max-width: 900px) { .ia-head { grid-template-columns: 1fr; } .ia-meta { text-align: left; } }
.ia-section { margin: 26px 0; }
.ia-section h2 { font-size: 12px; letter-spacing: .12em; text-transform: uppercase;
  margin: 0 0 12px; font-weight: 800; display: flex; align-items: baseline; gap: 10px; }
.ia-section h2 span { font-weight: 500; letter-spacing: 0; text-transform: none;
  color: var(--ia-muted); font-size: 12px; }
.ia-watch { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.ia-chip { display: inline-flex; align-items: center; gap: 7px; border: 2px solid var(--border);
  background: var(--card); padding: 4px 10px; font-size: 12px; box-shadow: 3px 3px 0 var(--border); }
.ia-chip .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--ia-muted); flex: none; }
.ia-chip.unf .dot { background: var(--ia-unf); }
.ia-chip.fav .dot { background: var(--ia-fav); }
.ia-chip.warn .dot { background: var(--ia-warn); }
.ia-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 14px 0 4px; }
.ia-kpis .kpi { border: 2px solid var(--border); background: var(--card); padding: 10px 12px 8px;
  box-shadow: 3px 3px 0 var(--border); display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.ia-kpis .kpi .l { font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--ia-muted);
  font-weight: 600; }
.ia-kpis .kpi .v { font-size: 26px; font-weight: 800; letter-spacing: -.02em; line-height: 1.15; }
.ia-kpis .kpi .d { font-size: 11px; display: flex; gap: 8px; flex-wrap: wrap; }
.ia-kpis .kpi .d b.fav { color: var(--ia-fav); } .ia-kpis .kpi .d b.unf { color: var(--ia-unf); }
.ia-kpis .kpi .s { font-size: 11px; color: var(--ia-muted); }
.ia-kpis .kpi svg { width: 100%; height: 34px; margin-top: 4px; color: #444; display: block; }
.ia-mid { display: grid; grid-template-columns: 1fr 1.2fr; gap: 18px; align-items: start; }
@media (max-width: 900px) { .ia-mid { grid-template-columns: 1fr; } }
.chart, .tblcard { border: 2px solid var(--border); background: var(--card); padding: 10px 12px 8px;
  box-shadow: 3px 3px 0 var(--border); margin: 14px 0; overflow-x: auto; }
.chart h3, .tblcard h3 { margin: 0 0 6px; font-size: 12px; font-weight: 800; letter-spacing: .02em; }
.chart h3 span, .tblcard h3 span { font-weight: 500; color: var(--ia-muted); }
.chart svg { width: 100%; height: 232px; display: block; }
.chart .ia-grid line { stroke: var(--ia-line-soft); stroke-width: 1; }
.chart .ia-grid text, .chart .ia-xaxis text { fill: var(--ia-muted); font-size: 10px; font-family: inherit; }
.legend { display: flex; gap: 14px; font-size: 11.5px; color: var(--ia-muted); margin-top: 6px; flex-wrap: wrap; }
.legend i { display: inline-block; width: 18px; height: 3px; vertical-align: middle; margin-right: 6px; }
.legend .muted { color: var(--ia-muted); }
.tblcard table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tblcard th { text-align: right; font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--ia-muted); font-weight: 600; padding: 6px 8px; border: none; border-bottom: 2px solid var(--border);
  white-space: nowrap; background: transparent; }
.tblcard th:first-child, .tblcard td:first-child { text-align: left; }
.tblcard td { padding: 6px 8px; border: none; border-bottom: 1px solid var(--ia-line-soft);
  text-align: right; white-space: nowrap; }
.tblcard td.lbl { text-align: left; }
.tblcard tr.sub td { font-weight: 700; border-top: 2px solid var(--border); background: var(--ia-paper-2); }
.tblcard tr.total td { font-weight: 800; border-top: 3px solid var(--border);
  border-bottom: 3px double var(--border); font-size: 13px; }
.tblcard tr.group td { text-align: left; font-weight: 800; font-size: 11px; letter-spacing: .1em;
  text-transform: uppercase; color: var(--ia-muted); background: var(--ia-paper-2); padding-top: 9px; }
.tblcard td .bar { display: inline-block; width: 18px; height: 9px; vertical-align: middle;
  margin-right: 6px; border: 1px solid var(--border); }
.tblcard td.pct { color: var(--ia-muted); }
.tblcard .fav { color: var(--ia-fav); } .tblcard .unf { color: var(--ia-unf); }
.tblcard .desc { text-align: left !important; white-space: normal !important; min-width: 220px; max-width: 360px; }
.tblcard .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.tblcard .muted { color: var(--ia-muted); font-size: 11px; margin-top: 8px; }
.ia-section details { margin-top: 10px; }
/* --ink (not --accent-ink): --accent-ink is the contrast color computed for text ON
   the --accent background (table headers, .fs-chip.fs-dark) -- this summary sits on
   the plain --card background with no --accent fill, so --accent-ink renders
   white-on-white whenever accent_hsl is dark (the default) -- see _accent_ink's own
   docstring and the identical print-media workaround for financial_statement above. */
.ia-section summary { cursor: default; font-size: 12px; font-weight: 700; color: var(--ink); list-style: none; }
.ia-section summary::before { content: "▸ "; }
.narr { border-left: 4px solid var(--accent); padding: 4px 0 4px 14px; max-width: 84ch; }
.narr p { margin: 0 0 8px; }
.hl { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 8px 22px;
  margin: 0; padding: 0; list-style: none; }
.hl li { font-size: 13px; padding-left: 14px; position: relative; }
.hl li::before { content: ""; position: absolute; left: 0; top: .55em; width: 7px; height: 7px;
  background: var(--ink); }
/* Render-polish brief item 6: "Sources & method" as the mock's labelled grid
   (Source / Snapshots used / Age (full width) / Queries / Integrity) -- this
   module's own styling, not the shared `.prov` plain-footer class every other
   report type still uses via `_provenance_html`. */
.ia-prov { font-size: 12px; color: var(--ia-muted); display: grid;
  grid-template-columns: 1fr 1fr; gap: 6px 24px; }
.ia-prov b { color: var(--ink); font-weight: 600; }
.ia-prov .full { grid-column: 1 / -1; }
.ia-prov .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
@media (max-width: 900px) { .ia-prov { grid-template-columns: 1fr; } }
/* Print (design rule #15): a bare <details> (no `open` attribute) collapses its content
   natively in every browser -- board packs need the FULL aged list on paper regardless
   of the on-screen collapse state (spec §A4 "the appendix (full aged list) is appended
   as its own pages"), so print forces every inventory_aging <details> block open. */
@media print {
  .ia-section details:not([open]) > * { display: block !important; }
  .ia-section summary { display: none !important; }
  .chart, .tblcard { overflow-x: visible; box-shadow: none; break-inside: avoid; }
}
"""


def fmt_amount(value) -> str:
    """Accounting-style format for a CURRENCY cell: thousands separators, 2 decimals
    (exact — the displayed lines foot to the total, no precision loss), negatives in
    parentheses (``5583749.13`` → ``"5,583,749.13"``, ``-4595824.07`` →
    ``"(4,595,824.07)"``). ``None`` and non-finite floats (NaN/Inf) → empty string;
    non-numeric values (and bools) are returned via ``str()`` unchanged.

    Applied ONLY to columns the producer tags as currency (``currency_columns``) — the
    table renderer is shared infrastructure, so a generic numeric column (year, ratio,
    count, id) must NOT be accounting-formatted ('is a number' ≠ 'is a dollar amount').
    """
    if value is None:
        return ""
    # bool is an int subclass — never format True/False as 1/0.
    if isinstance(value, bool):
        return str(value)
    # Resolve the value to an EXACT Decimal. Currency cells may arrive as a number
    # (reportData floats) OR a STRING (SuiteQL serializes amounts as text, often in
    # scientific notation). Parse via Decimal — binary float() corrupts >15-significant-
    # digit amounts and half-cents (e.g. "999999999999999.99" → off a dollar, "2.675" →
    # 2.67). `overflow_fallback` is what we render if the value can't be quantized to
    # cents: a string → verbatim (never blank a real figure), a number → blank.
    if isinstance(value, str):
        s = value.strip()
        # Coerce ONLY a string that strictly matches a US-format amount; anything else
        # (locale-formatted, mis-grouped, zero-padded code, underscore/sentinel, or
        # non-numeric like "N/A") passes through VERBATIM — never reformat a value we
        # can't safely parse into a possibly-wrong dollar figure.
        if not _AMOUNT_STR_RE.match(s):
            return value
        try:
            d = Decimal(s.replace(",", ""))
        except InvalidOperation:
            return value
        overflow_fallback = value  # a string we can't quantize → verbatim
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""  # an actual float NaN/Inf (a computed/undefined value) → blank
        d = Decimal(str(value))  # via str() to avoid binary-float repr noise
        # An absurdly-large finite magnitude that won't quantize → its raw repr. Use
        # str() NOT f"{value:,.2f}" — the latter raises OverflowError on a >309-digit
        # int (int→float) and binary-float-corrupts a large int's digits.
        overflow_fallback = str(value)
    else:
        return str(value)
    try:
        # Generous precision so any realistic amount (and large-but-finite cases like
        # 1e26) quantizes — the default Decimal context (prec 28) would blank a finite
        # value with ~26+ integer digits. A truly out-of-range value (e.g. "1e400")
        # still raises and falls back, never silently dropping a figure.
        with localcontext() as ctx:
            ctx.prec = 38
            q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return overflow_fallback
    body = f"{abs(q):,.2f}"
    return f"({body})" if q < 0 else body


def _coerce_total(raw) -> int | None:
    """Coerce a table section's ``row_count`` to an int for the disclosure notes —
    it may arrive as an int OR a numeric string (some MCP shapes); bools never count."""
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _md_inline(text: str) -> str:
    # Minimal: escape, then **bold**. (No raw HTML passthrough — trust boundary + XSS safety.)
    import re

    esc = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)


def _split_row(line: str) -> list[str]:
    # "| a | b |" -> ["a", "b"]. Tolerates missing edge pipes; drops the empty
    # cells produced by leading/trailing pipes.
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _is_delimiter_row(line: str) -> bool:
    import re

    # A GFM delimiter row always contains a pipe (outer `|---|` or inner `---|---`).
    # Requiring one keeps a bare `---` thematic break / setext underline from being
    # mistaken for a table delimiter and swallowing the preceding line.
    if "|" not in line:
        return False
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c or "") for c in cells)


def _md_block(text: str) -> str:
    # Block-level markdown for narrative content. Renders GFM tables as real
    # <table>s and blank-line-separated prose as <p>. Everything is escaped via
    # _md_inline — no raw HTML passthrough (trust boundary + XSS safety).
    lines = text.split("\n")
    out: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            # Single newlines reflow (GFM treats them as a space), matching the
            # prior whitespace-collapsing behavior — no injected hard breaks.
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # GFM table: a header row followed by a delimiter row.
        if "|" in line and i + 1 < n and _is_delimiter_row(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            width = len(header)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                # Normalize each row to the header width (GFM: pad short, drop extra).
                cells = _split_row(lines[i])
                cells = (cells + [""] * width)[:width]
                rows.append(cells)
                i += 1
            head = "".join(f"<th>{_md_inline(c)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue
        if line.strip() == "":
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    return "".join(out)


def _section_html(s: dict) -> str:
    t = s.get("type")
    if t == "heading":
        lvl = min(max(int(s.get("level", 2)), 1), 3)
        return f"<h{lvl}>{escape(str(s.get('text', '')))}</h{lvl}>"
    if t == "narrative":
        # Task 2 (Slice 1): a `model` key (an inventory_aging `Narrative`) means THIS
        # is the bespoke inventory_aging narrative card; every existing caller passes
        # `markdown` and never `model`, so that path is completely untouched — byte-
        # identical to before this branch existed.
        if "model" in s:
            return _ia_narrative_html(s["model"])
        return f'<div class="nb-card svg-wrap">{_md_block(str(s.get("markdown", "")))}</div>'
    if t == "report_head":
        return _ia_report_head_html(s.get("model") or {})
    if t == "watch_items":
        return _ia_watch_html(s.get("model") or ())
    if t == "kpi_cards":
        return _ia_kpi_cards_html(s.get("model") or ())
    if t == "trend_chart":
        model = s.get("model")
        return _ia_trend_chart_html(model) if model is not None else ""
    if t == "variance_table":
        model = s.get("model")
        return _ia_variance_table_html(model) if model is not None else ""
    if t == "mid_row":
        model = s.get("model")
        return _ia_mid_row_html(model) if model is not None else ""
    if t == "bucket_table":
        model = s.get("model")
        return _ia_bucket_table_html(model) if model is not None else ""
    if t == "top_positions":
        model = s.get("model")
        return _ia_top_positions_html(model) if model is not None else ""
    if t == "highlights":
        return _ia_highlights_html(s.get("model") or ())
    if t == "provenance_grid":
        model = s.get("model")
        return _ia_provenance_html(model) if model is not None else ""
    if t == "metric_headline":
        foot = ""
        if s.get("definition_version") is not None:
            version = escape(str(s["definition_version"]))
            period = escape(str(s.get("period", "")))
            foot = f'<span class="foot">definition v{version} · {period}</span>'
        return (
            f'<div class="nb-card metric"><span class="label">{escape(str(s.get("label", "")))}</span>'
            f'<span class="value">{escape(str(s.get("value", "")))} '
            f"<small>{escape(str(s.get('unit', '')))}</small></span>{foot}</div>"
        )
    if t == "chart":
        return f'<div class="nb-card svg-wrap">{s.get("svg", "")}</div>'  # svg is server-generated, trusted
    if t == "table":
        columns = s.get("columns", [])
        rows = s.get("rows", [])
        ncols = len(columns)
        # Accounting formatting is scoped to columns the PRODUCER tags as currency
        # (e.g. the reportData "amount" column) — NOT guessed from value type. The table
        # renderer is shared by SuiteQL/BigQuery/recon/etc., so a generic numeric column
        # (year, ratio, count, id) must render raw, never comma-grouped/rounded.
        currency = set(s.get("currency_columns") or [])

        def _num_cls(i: int) -> str:
            return ' class="num"' if i < ncols and columns[i] in currency else ""

        cols = "".join(f"<th{_num_cls(i)}>{escape(str(c))}</th>" for i, c in enumerate(columns))
        body_rows = []
        for row in rows:
            # Render max(ncols, len(row)) cells: pad a short row, but NEVER silently drop
            # the trailing values of an over-wide row (that would hide a real figure).
            cells = []
            for i in range(max(ncols, len(row))):
                v = row[i] if i < len(row) else None
                if i < ncols and columns[i] in currency:
                    # fmt_amount handles None/non-finite → "" and non-numeric → str().
                    cells.append(f'<td class="num">{escape(fmt_amount(v))}</td>')
                elif v is None:
                    cells.append("<td></td>")  # null → empty cell, never "None"
                else:
                    cells.append(f"<td>{escape(str(v))}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        body = "".join(body_rows)
        note = ""
        # A statement-curated table is not a positional "first N" slice — it shows the
        # named section-summary lines. Disclose the curation (and the true source size)
        # with wording that matches what was actually done. Same total coercion as the
        # truncated branch (row_count may be a numeric STRING in some MCP shapes).
        if s.get("curation") == "statement":
            total = _coerce_total(s.get("row_count"))
            of_total = f" from {escape(str(total))} source rows" if total is not None and total > len(rows) else ""
            note = f'<p class="foot">Curated statement — {len(rows)} summary lines{of_total}.</p>'
        # A truncated section MUST disclose it (never render a partial financial table as
        # whole). When the true total is known and exceeds the shown rows, name it; when
        # the upstream reported row_count == shown (e.g. NetSuite-side fetch truncation,
        # true total unknown), still disclose without a contradictory "first N of N".
        elif s.get("truncated"):
            total = _coerce_total(s.get("row_count"))
            if total is not None and total > len(rows):
                note = f'<p class="foot">Showing first {len(rows)} of {escape(str(total))} rows.</p>'
            else:
                note = f'<p class="foot">Showing first {len(rows)} rows (results truncated).</p>'
        return (
            # table-wrap = the capped-height scroll region the sticky thead binds to
            # (Slice D); charts/narratives keep the plain svg-wrap.
            f'<div class="nb-card svg-wrap table-wrap"><table><thead><tr>{cols}</tr></thead>'
            f"<tbody>{body}</tbody></table>{note}</div>"
        )
    if t == "financial_statement":
        return _financial_statement_html(s.get("model") or {})
    if t == "divider":
        return '<div class="divider"></div>'
    if t == "error":
        return (
            '<div class="nb-card" style="border-color:#ef4444">'
            f"<strong>Data unavailable:</strong> {escape(str(s.get('reason', '')))}</div>"
        )
    return ""


def _fmt_stamp(iso: str) -> str:
    """Human date for the freshness stamp: ``"6 Jul 2026, 14:05 UTC"``. An unparseable
    value renders escape()d verbatim — the stamp is honesty metadata; never crash or
    silently drop it."""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return escape(str(iso))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return escape(f"{dt.day} {dt.strftime('%b %Y, %H:%M')} UTC")


def _accent_ink(accent_hsl: str) -> str:
    """Table-header text color readable on the accent background (live QA 2026-07-09:
    a near-black tenant accent rendered dark-on-dark, illegible headers): white on a
    dark accent, near-black otherwise. Computed server-side from the hsl lightness —
    CSS alone cannot derive a contrast color from an hsl() custom property.
    Unparseable → dark ink (safe on the light default card)."""
    m = re.search(r"(\d+(?:\.\d+)?)%\s*\)?\s*$", accent_hsl or "")
    if not m:
        return "#111"
    return "#fff" if float(m.group(1)) < 55 else "#111"


# A recipe source's tool name for an external MCP call: "ext__<32-hex connection
# fingerprint>__<raw tool name>". Distinguishes MCP-routed sources (label by raw tool
# name) from local tools (labeled via _TOOL_LABELS below).
_EXT_TOOL_RE = re.compile(r"^ext__[0-9a-f]{32}__(?P<raw>.+)$")

_TOOL_LABELS = {
    "netsuite_financial_report": "NetSuite GL statement template (SuiteQL)",
    "netsuite_suiteql": "NetSuite SuiteQL query",
}


# Params that must never reach the frozen HTML's "Sources & method" block. All of these
# are full SQL text (or verbatim chat text) on tools that ARE recipe-eligible — a
# recipe-eligible tool means a real captured recipe can carry the param, so each must be
# named here regardless of which tool/key shape carries it:
# - `query` (local netsuite_suiteql) and `sqlQuery` (external ext__..__ns_runCustomSuiteQL /
#   ns_runSuiteQL — the external-MCP equivalent key) are the literal SQL text. The label
#   ("NetSuite SuiteQL query" / "External MCP tool ...") already conveys method; printing
#   SQL into a report is its own trust-boundary problem regardless of content.
# - `left_query` / `right_query` (cross_source_query) are the same leak class: two full
#   SQL texts on a different recipe-eligible tool.
# - LLM-only params are additionally stripped per-tool via refresh_service's
#   `_LLM_ONLY_PARAMS` (the set stripped before dispatch on refresh) — a captured
#   `user_question` is verbatim chat text, and echoing it here would both leak arbitrary
#   user text into every recipe-bearing report AND misrepresent the replay (refresh never
#   actually sends it to the tool).
_ALWAYS_EXCLUDED_PARAM_KEYS = frozenset({"query", "sqlQuery", "left_query", "right_query"})

# Forward guard: even a param that survives the exclusion list above must not blow up the
# frozen HTML with an unbounded value — a future recipe-eligible tool could carry a big
# text param under a name not yet on the list. Caps, doesn't hide: the key still shows.
_DETAIL_VALUE_MAX_LEN = 80


def _truncate_detail_value(value: object) -> str:
    s = str(value)
    return s if len(s) <= _DETAIL_VALUE_MAX_LEN else s[:_DETAIL_VALUE_MAX_LEN] + "…"


def build_provenance(sources: dict, executed_at: str, *, resolved_rids: set[str] | None = None) -> list[dict]:
    """Translate a recipe's raw ``sources`` map (``result_id -> {tool, params, ...}``)
    into human-readable entries for the renderer's "Sources & method" block: each result
    id, a plain-English label for the tool that produced it, its params as ``detail``,
    and when it ran. Sorted by ``result_id`` for deterministic (byte-stable) output.

    ``detail`` is policy-filtered (see ``_ALWAYS_EXCLUDED_PARAM_KEYS`` / ``_LLM_ONLY_PARAMS``
    above) and each surviving value length-capped (``_DETAIL_VALUE_MAX_LEN``) — never a raw
    dump of every captured param. Playbook sources (``report_type``, ``period``) and
    external-MCP params (e.g. ``reportId``) are unaffected.

    ``resolved_rids`` (T2 gate M1 — provenance honesty): a ``financial_statement``'s
    compare-degrade seam can omit a rid from the ``payloads`` a run actually resolved
    (a failed/unavailable prior/yoy/trend source) — WITHOUT this param, every source in
    ``sources`` renders as "executed", including one that never actually ran this time, a
    false trust claim. When given, a rid NOT in this set is marked unresolved: no
    ``executed_at`` stamp (it did NOT run), and ``_provenance_html`` renders a distinct
    "not available this run" line for it instead. Default ``None`` = every rid is treated
    as resolved — BYTE-IDENTICAL to this function's behavior before this param existed,
    so a caller that doesn't pass it sees no change at all."""
    from app.services.report.refresh_service import _LLM_ONLY_PARAMS

    entries = []
    for result_id in sorted(sources):
        src = sources[result_id] or {}
        tool = str(src.get("tool", ""))
        m = _EXT_TOOL_RE.match(tool)
        if m:
            raw = m.group("raw")
            label = "NetSuite native report runner" if raw == "ns_runReport" else f"External MCP tool {raw}"
        else:
            label = _TOOL_LABELS.get(tool, tool)
        params = src.get("params") or {}
        excluded = _ALWAYS_EXCLUDED_PARAM_KEYS | _LLM_ONLY_PARAMS.get(tool, frozenset())
        detail = ", ".join(f"{k}={_truncate_detail_value(params[k])}" for k in sorted(params) if k not in excluded)
        resolved = resolved_rids is None or result_id in resolved_rids
        entries.append(
            {
                "result_id": result_id,
                "label": label,
                "detail": detail,
                "executed_at": executed_at if resolved else None,
                "resolved": resolved,
            }
        )
    return entries


def _provenance_line_html(p: dict) -> str:
    rid = escape(str(p.get("result_id", "")))
    label = escape(str(p.get("label", "")))
    detail = escape(str(p.get("detail", "")))
    if p.get("resolved", True):
        return f"<div>{rid} — {label} · {detail} · executed {escape(str(p.get('executed_at', '')))}</div>"
    # T2 gate M1: no executed_at stamp -- it did NOT run this time, never claim it did.
    return f"<div>{rid} — {label} · {detail} · not available this run — comparison omitted</div>"


def _provenance_html(provenance: list[dict]) -> str:
    rows = "".join(_provenance_line_html(p) for p in provenance)
    return (
        '<div class="prov"><strong>Sources &amp; method</strong>'
        f"{rows}"
        "<div>Numbers are tool-computed and rendered deterministically — no model generated a figure.</div>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Task 3 — `financial_statement` section renderer.
#
# Consumes ONLY `section["model"]` — the render-ready dict statement_builder.
# build_statement_model produces (see that module's docstring for the exact shape).
# Every number actually PRINTED comes from a pre-formatted string already on the model;
# the only raw Decimals touched here are `kpis[].spark` / `trend.series[].values`, and
# only for SVG geometry (point placement) plus the chart <title> tooltip text the brief
# specifies verbatim — never a new derived financial figure. Every model string is
# escape()d on output, including account names and narrative/highlight/watch text.
# ---------------------------------------------------------------------------

# Same typographic minus statement_builder.MINUS formats negatives with — kept as an
# independent literal (not imported) since this is presentation-only sign detection on an
# already-formatted string, not a dependency on the builder's internals.
_MINUS = "−"

# income_statement produces 5 sections (Revenue/Other Income/COGS/OpEx/Other Expense) —
# the most any current statement type produces (balance_sheet: 3, trial_balance: 1). A
# section past this cap still renders (never truncated) but loses the collapse checkbox —
# same degrade-not-truncate pattern as report_charts._MAX_TOGGLE_SERIES. A drift test
# binds the CSS :has() rule count to this constant.
_MAX_STATEMENT_SECTIONS = 5

# EYEBALL-GATE FIX (F2, design rule #6): a two-step GAAP income statement interleaves
# formula rows BETWEEN sections (Revenue -> COGS -> Gross Profit -> OpEx -> Operating
# Income -> Other Income -> Other Expense -> Net Income) rather than stacking them all
# after the last section. This DISPLAY order differs from statement_builder's internal
# section-KEY grouping order (1-Revenue, 2-Other Income, 3-COGS, 4-Operating Expense,
# 5-Other Expense -- the SuiteQL/model grouping order, an unrelated concern nothing else
# depends on). Deliberately a renderer-only presentation concern, NOT a builder change:
# statement_builder's section-key order stays stable for every other consumer.
_IS_SECTION_DISPLAY_ORDER = ["1-Revenue", "3-COGS", "4-Operating Expense", "2-Other Income", "5-Other Expense"]
# Index into model["formulas"] (always [gross_profit_row, operating_income_row] for
# income_statement) to insert immediately after finishing the section at this key.
_IS_FORMULA_INSERT_AFTER = {"3-COGS": 0, "4-Operating Expense": 1}

_FS_KPI_SPARK_COLORS = {
    "revenue": "#4348c8",
    "gross_profit": "#111111",
    "operating_income": "#111111",
    "net_income": "#0A7A3D",
}
_FS_TREND_COLORS = {
    "revenue": "#4348c8",
    "gross_profit": "#111111",
    "operating_income": "#6b46c1",
    "net_income": "#0A7A3D",
}
_STATEMENT_TITLE_MAP = {
    "income_statement": "Statement of Income",
    "balance_sheet": "Balance Sheet",
    "trial_balance": "Trial Balance",
}
_WATCH_TONE_CLASSES = {"good": "fs-good", "warn": "fs-warn", "bad": "fs-bad"}

# SVG geometry constants (px). Chart size/shape is a fixed presentational choice, not
# derived from the model.
_FS_SPARK_W, _FS_SPARK_H = 64, 26
_FS_TREND_W, _FS_TREND_H = 620, 240
# Right pad 28 (final-review minor): the last x-axis label is CENTERED (text-anchor="middle")
# on its point, so it extends roughly HALF its own rendered width past that x position -- a
# realistic period label ("Jun 2026") was wider than the old 16px pad, clipping ~2 characters
# against the SVG's right edge.
_FS_TREND_PAD_L, _FS_TREND_PAD_R, _FS_TREND_PAD_T, _FS_TREND_PAD_B = 56, 28, 24, 34


def _fs_sign_tone(text: str, *, neutral: bool = False) -> tuple[str, str]:
    """(tone_class, arrow) from an already-FORMATTED delta string's own sign — used for
    KPI headline cards. An IS KPI (revenue, gross/operating/net income) moving up is
    inherently favorable. A BS/TB KPI (assets/liabilities/equity, debits/credits) has NO
    such inherent favorability (design rule #10: color is reserved EXCLUSIVELY for
    favorable/unfavorable, never decoration) — ``neutral=True`` keeps the arrow (still
    informative: which way did it move) but always returns tone ``""`` (no color class).
    ``""`` tone ALSO covers a zero delta regardless of ``neutral`` (nothing moved)."""
    if text in ("$0", "0.0%", "0.0pp"):
        return "", "•"
    if text.startswith(_MINUS):
        return ("", "▼") if neutral else ("fs-bad", "▼")
    return ("", "▲") if neutral else ("fs-good", "▲")


def _fs_delta_tone(delta: str | None, reduces_profit: bool) -> str:
    """Favorability for a LINE-ITEM delta (statement account/subtotal/formula/quad rows),
    derived from the model's own ``reduces_profit`` flag rather than sign alone — an
    increasing expense (reduces_profit=True) is unfavorable even though its delta is
    positive, matching the parens/reduces_profit convention already baked into the model
    (design rule #10: color reflects favorability, not raw sign). ``""`` = no color
    (zero delta, or delta unavailable)."""
    if not delta or delta == "$0":
        return ""
    increased = not delta.startswith(_MINUS)
    favorable = increased != reduces_profit
    return "fs-good" if favorable else "fs-bad"


def _fs_sparkline_svg(values, color: str) -> str:
    """A compact KPI-card sparkline (mock: 64x26, polyline + endpoint dot). ``None`` or
    a single-point series renders nothing (a sparkline needs >=2 points to show a trend).
    """
    if not values or len(values) < 2:
        return ""
    floats = [float(v) for v in values]
    vmin, vmax = min(floats), max(floats)
    span = (vmax - vmin) or 1.0
    step = _FS_SPARK_W / (len(floats) - 1)
    pts = [(i * step, _FS_SPARK_H - ((v - vmin) / span) * _FS_SPARK_H) for i, v in enumerate(floats)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    ex, ey = pts[-1]
    return (
        f'<svg class="fs-spark" width="{_FS_SPARK_W}" height="{_FS_SPARK_H}" '
        f'viewBox="0 0 {_FS_SPARK_W} {_FS_SPARK_H}" aria-hidden="true">'
        f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3" fill="{color}"/></svg>'
    )


def _fs_kpi_sub_html(kpi: dict) -> str:
    bits = []
    margin_pct = kpi.get("margin_pct")
    if margin_pct is not None:
        bits.append(f"{escape(str(margin_pct))} margin")
    mom_pct = kpi.get("mom_pct")
    mom_delta = kpi.get("mom_delta")
    mom_text = mom_pct if mom_pct is not None else mom_delta
    if mom_text is not None:
        tone, arrow = _fs_sign_tone(mom_text, neutral=bool(kpi.get("neutral")))
        cls = f" {tone}" if tone else ""
        bits.append(f'<span class="fs-delta{cls}">{arrow} {escape(str(mom_text))} MoM</span>')
    yoy_pct = kpi.get("yoy_pct")
    if yoy_pct is not None:
        bits.append(f"{escape(str(yoy_pct))} YoY")
    return " · ".join(bits)


def _fs_kpi_html(kpi: dict) -> str:
    label = escape(str(kpi.get("label", "")))
    value = escape(str(kpi.get("value", "")))
    sub = _fs_kpi_sub_html(kpi)
    sub_html = f'<div class="fs-kpi-sub num">{sub}</div>' if sub else ""
    spark_color = _FS_KPI_SPARK_COLORS.get(kpi.get("key"), "#111111")
    spark_html = _fs_sparkline_svg(kpi.get("spark"), spark_color)
    return (
        f'<div class="nb-card fs-kpi"><div class="fs-kpi-lbl">{label}</div>'
        f'<div class="fs-kpi-val num">{value}</div>{sub_html}{spark_html}</div>'
    )


def _fs_axis_label(v: float) -> str:
    """Compact axis-gridline text ("$14M"/"$7M"/"$0") — presentational chart geometry
    labeling (mirrors report_charts._fmt), never the authoritative displayed figure."""
    sign = "-" if v < 0 else ""
    av = abs(v)
    if av >= 1_000_000:
        return f"{sign}${av / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{sign}${av / 1_000:.1f}K"
    return f"{sign}${av:,.0f}"


def _fs_tip_value(v: Decimal) -> str:
    """Exact-value chart tooltip text per the brief: ``"{period} — {series}: ${value:,}"``.
    Presentational tooltip formatting of an already-given raw Decimal, not a derived
    financial figure — the model's pre-formatted strings remain authoritative for every
    number actually printed in the KPI/quad/statement tables."""
    q = v.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    sign = _MINUS if q < 0 else ""
    return f"{sign}${abs(q):,}"


def _fs_trend_html(trend: dict | None) -> str:
    if not trend or not trend.get("periods") or not trend.get("series"):
        return ""
    periods = trend["periods"]
    series = trend["series"]
    n = len(periods)
    if n < 2:
        return ""
    plot_w = _FS_TREND_W - _FS_TREND_PAD_L - _FS_TREND_PAD_R
    plot_h = _FS_TREND_H - _FS_TREND_PAD_T - _FS_TREND_PAD_B
    bottom = _FS_TREND_PAD_T + plot_h
    # Filter to the series that will ACTUALLY plot (matching length) BEFORE computing the
    # axis scale (T2 gate minor[7]) — a malformed/partial series must never distort
    # vmax/vmin for the legitimate ones just because it's present in the input.
    plotted_series = [s for s in series if len(s.get("values") or []) == n]
    all_vals = [float(v) for s in plotted_series for v in (s.get("values") or [])]
    vmax = max([*all_vals, 0.0])
    vmin = min([*all_vals, 0.0])
    span = (vmax - vmin) or 1.0

    def y_of(v: float) -> float:
        return _FS_TREND_PAD_T + plot_h * (vmax - v) / span

    step = plot_w / max(n - 1, 1)
    parts = [
        f'<line x1="{_FS_TREND_PAD_L}" y1="{bottom:.1f}" x2="{_FS_TREND_W - _FS_TREND_PAD_R}" y2="{bottom:.1f}" '
        'stroke="#000" stroke-width="2"/>'
    ]
    for frac in (0.0, 1.0):
        y = _FS_TREND_PAD_T + plot_h * frac
        label_v = vmax if frac == 0.0 else vmin
        parts.append(
            f'<text x="{_FS_TREND_PAD_L - 8}" y="{y + 4:.1f}" font-size="10" text-anchor="end" fill="#666">'
            f"{escape(_fs_axis_label(label_v))}</text>"
        )
        if frac > 0:
            parts.append(
                f'<line x1="{_FS_TREND_PAD_L}" y1="{y:.1f}" x2="{_FS_TREND_W - _FS_TREND_PAD_R}" y2="{y:.1f}" '
                'stroke="#e4e1d8" stroke-width="1"/>'
            )
    legend_items: list[tuple[str, str]] = []
    for s in plotted_series:
        values = s.get("values") or []
        color = _FS_TREND_COLORS.get(s.get("key"), "#111111")
        pts = [(_FS_TREND_PAD_L + i * step, y_of(float(v))) for i, v in enumerate(values)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="3"/>')
        series_label = str(s.get("label", ""))
        for i, (x, y) in enumerate(pts):
            title = f"{periods[i]} — {series_label}: {_fs_tip_value(values[i])}"
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="#000" stroke-width="1.5">'
                f"<title>{escape(title)}</title></circle>"
            )
        legend_items.append((series_label, color))
    for i, p in enumerate(periods):
        x = _FS_TREND_PAD_L + i * step
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 18:.1f}" font-size="11" text-anchor="middle" fill="#444">'
            f"{escape(str(p))}</text>"
        )
    if not legend_items:
        return ""
    # width=100% + a FIXED pixel height would letterbox: the browser scales content to
    # fit within (containerWidth x 240px) preserving the viewBox aspect ratio, leaving
    # blank space above/below whenever the narrower grid column shrinks containerWidth
    # below the viewBox's natural ~2.6:1 ratio. style="height:auto" (the responsive-SVG
    # technique) makes height follow width instead, so the chart always fills its card.
    svg = (
        f'<svg width="{_FS_TREND_W}" height="{_FS_TREND_H}" viewBox="0 0 {_FS_TREND_W} {_FS_TREND_H}" '
        f'style="width:100%;height:auto;display:block" role="img" aria-label="Statement trend">'
        f"{''.join(parts)}</svg>"
    )
    legend_html = "".join(
        f'<span><span class="fs-sw" style="background:{color}"></span>{escape(label)}</span>'
        for label, color in legend_items
    )
    return (
        '<div class="nb-card"><h3>Trend <small>· exact values on hover</small></h3>'
        f'<div class="fs-scroll">{svg}</div><div class="fs-legend">{legend_html}</div></div>'
    )


def _fs_summary_row_html(row: dict, row_cls: str, *, has_prior: bool, has_pct_rev: bool) -> str:
    """A subtotal/formula/net/quad row — a ``_quad_row``-shaped model dict (label/current/
    prior/delta/reduces_profit/pct_rev). ``pct_rev`` (design rule #8, T2 gate M3) renders
    exactly like an account row's when the model supplies one (IS section subtotals,
    Gross Profit/Operating Income, Net Income) — blank ONLY when the model itself has
    None (BS/TB, which have no revenue base), never invented here."""
    label = escape(str(row.get("label", "")))
    current = escape(str(row.get("current", "")))
    cells = [f"<td>{label}</td>", f'<td class="num">{current}</td>']
    if has_prior:
        prior = row.get("prior")
        delta = row.get("delta")
        tone = _fs_delta_tone(delta, bool(row.get("reduces_profit")))
        tone_cls = f' class="num {tone}"' if tone else ' class="num"'
        cells.append(f'<td class="num">{escape(str(prior)) if prior is not None else ""}</td>')
        cells.append(f"<td{tone_cls}>{escape(str(delta)) if delta is not None else ''}</td>")
    if has_pct_rev:
        pct_rev = row.get("pct_rev")
        cells.append(f'<td class="num fs-pct">{escape(str(pct_rev)) if pct_rev is not None else ""}</td>')
    return f'<tr class="{row_cls}">{"".join(cells)}</tr>'


def _fs_account_row_html(acct: dict, sec_idx: int, *, has_prior: bool, has_pct_rev: bool) -> str:
    number = escape(str(acct.get("number", "")))
    name = escape(str(acct.get("name", "")))
    current = escape(str(acct.get("current", "")))
    cells = [f'<td><span class="fs-acct-no">{number}</span>{name}</td>', f'<td class="num">{current}</td>']
    if has_prior:
        prior = acct.get("prior")
        delta = acct.get("delta")
        tone = _fs_delta_tone(delta, bool(acct.get("reduces_profit")))
        tone_cls = f' class="num {tone}"' if tone else ' class="num"'
        cells.append(f'<td class="num">{escape(str(prior)) if prior is not None else ""}</td>')
        cells.append(f"<td{tone_cls}>{escape(str(delta)) if delta is not None else ''}</td>")
    if has_pct_rev:
        pct_rev = acct.get("pct_rev")
        cells.append(f'<td class="num fs-pct">{escape(str(pct_rev)) if pct_rev is not None else ""}</td>')
    # Every account row still renders past the collapse cap (never truncated) — it just
    # loses the fs-of-N hide hook, since no CSS rule exists to bind it to (see
    # _MAX_STATEMENT_SECTIONS).
    cls = f"fs-acct fs-of-{sec_idx}" if sec_idx < _MAX_STATEMENT_SECTIONS else "fs-acct"
    return f'<tr class="{cls}">{"".join(cells)}</tr>'


def _fs_section_header_html(label: str, sec_idx: int, ncols: int) -> str:
    label_esc = escape(str(label))
    if sec_idx < _MAX_STATEMENT_SECTIONS:
        inner = (
            f'<label class="fs-sec-lbl"><input type="checkbox" class="fs-sec-cb fs-sec-{sec_idx}" checked>'
            f'<span class="car">▾</span> {label_esc}</label>'
        )
    else:
        inner = f'<span class="car">▾</span> {label_esc}'
    return f'<tr class="fs-sec"><td colspan="{ncols}">{inner}</td></tr>'


def _fs_quad_html(model: dict) -> str:
    quad = model.get("quad") or []
    if not quad:
        return ""
    has_prior = quad[0].get("prior") is not None
    period = escape(str(model.get("period", "")))
    prior_period = str(model.get("prior_period") or "")
    headers = ["<th>Metric</th>", f"<th>{period}</th>"]
    if has_prior:
        headers += [f"<th>{escape(prior_period)}</th>", "<th>Δ $</th>", "<th>Δ %</th>"]
    if has_prior:
        # Δ% needs its own cell (the shared _fs_summary_row_html helper only emits
        # current/prior/delta$) — quad rows are the only place delta_pct is displayed, so
        # a dedicated row renderer rather than growing the shared helper for one caller.
        rows = "".join(_fs_quad_row_with_pct_html(r) for r in quad)
    else:
        rows = "".join(
            _fs_summary_row_html(r, f"fs-{r.get('emph')}" if r.get("emph") else "", has_prior=False, has_pct_rev=False)
            for r in quad
        )
    title = f"Variance vs {escape(prior_period)}" if has_prior else "Variance"
    return (
        f'<div class="nb-card"><h3>{title} <small>· the four-column read</small></h3>'
        f'<div class="fs-scroll"><table class="fs-quad num"><thead><tr>{"".join(headers)}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div></div>"
    )


def _fs_quad_row_with_pct_html(row: dict) -> str:
    label = escape(str(row.get("label", "")))
    current = escape(str(row.get("current", "")))
    prior = row.get("prior")
    delta = row.get("delta")
    delta_pct = row.get("delta_pct")
    tone = _fs_delta_tone(delta, bool(row.get("reduces_profit")))
    tone_cls = f' class="num {tone}"' if tone else ' class="num"'
    emph = row.get("emph")
    row_cls = f"fs-{emph}" if emph else ""
    cells = (
        f"<td>{label}</td>"
        f'<td class="num">{current}</td>'
        f'<td class="num">{escape(str(prior)) if prior is not None else ""}</td>'
        f"<td{tone_cls}>{escape(str(delta)) if delta is not None else ''}</td>"
        f"<td{tone_cls}>{escape(str(delta_pct)) if delta_pct is not None else ''}</td>"
    )
    return f'<tr class="{row_cls}">{cells}</tr>'


def _fs_statement_table_html(model: dict) -> str:
    sections = model.get("sections") or []
    if not sections:
        return ""
    has_prior = model.get("prior_period") is not None
    has_pct_rev = any(a.get("pct_rev") is not None for sec in sections for a in sec.get("accounts", []))
    period = escape(str(model.get("period", "")))
    prior_period = str(model.get("prior_period") or "")
    headers = ["<th>Account</th>", f"<th>{period}</th>"]
    if has_prior:
        headers += [f"<th>{escape(prior_period)}</th>", "<th>Δ $</th>"]
    if has_pct_rev:
        headers.append("<th>% of rev</th>")
    ncols = len(headers)

    formulas = model.get("formulas") or []
    net = model.get("net")
    is_income_statement = model.get("statement") == "income_statement"

    if is_income_statement:
        by_key = {sec.get("key"): sec for sec in sections}
        present_keys = set(by_key)
        # Two-step display order (see _IS_SECTION_DISPLAY_ORDER); a section key not in
        # the known map (a future/unexpected type) still renders, appended at the end —
        # never silently dropped.
        ordered_sections = [by_key[k] for k in _IS_SECTION_DISPLAY_ORDER if k in by_key]
        ordered_sections += [sec for sec in sections if sec.get("key") not in _IS_SECTION_DISPLAY_ORDER]
    else:
        ordered_sections = sections
        present_keys = set()

    body_rows: list[str] = []
    for sec_idx, sec in enumerate(ordered_sections):
        body_rows.append(_fs_section_header_html(sec.get("label", ""), sec_idx, ncols))
        for acct in sec.get("accounts", []):
            body_rows.append(_fs_account_row_html(acct, sec_idx, has_prior=has_prior, has_pct_rev=has_pct_rev))
        body_rows.append(_fs_summary_row_html(sec["subtotal"], "fs-sub", has_prior=has_prior, has_pct_rev=has_pct_rev))
        if is_income_statement:
            formula_idx = _IS_FORMULA_INSERT_AFTER.get(sec.get("key"))
            if formula_idx is not None and formula_idx < len(formulas):
                body_rows.append(
                    _fs_summary_row_html(
                        formulas[formula_idx], "fs-formula", has_prior=has_prior, has_pct_rev=has_pct_rev
                    )
                )

    if is_income_statement:
        # Guarantee every formula row renders even in a degenerate fixture where its
        # anchor section (COGS/OpEx) has zero accounts and so never appears in
        # `sections` at all — appended here rather than silently dropped. No current
        # fixture exercises this (income_statement always has all 5 sections + both
        # formula rows), but the renderer must never lose a figure regardless.
        for idx, formula_row in enumerate(formulas):
            anchor_present = any(k in present_keys for k, v in _IS_FORMULA_INSERT_AFTER.items() if v == idx)
            if not anchor_present:
                body_rows.append(
                    _fs_summary_row_html(formula_row, "fs-formula", has_prior=has_prior, has_pct_rev=has_pct_rev)
                )
    else:
        for formula_row in formulas:
            body_rows.append(
                _fs_summary_row_html(formula_row, "fs-formula", has_prior=has_prior, has_pct_rev=has_pct_rev)
            )

    if net is not None:
        body_rows.append(_fs_summary_row_html(net, "fs-net", has_prior=has_prior, has_pct_rev=has_pct_rev))
    for check in model.get("checks") or []:
        tone = "fs-good" if check.get("ok") else "fs-bad"
        mark = "✓" if check.get("ok") else "✗"
        text = f"{mark} {escape(str(check.get('label', '')))} — {escape(str(check.get('detail', '')))}"
        body_rows.append(f'<tr class="fs-check {tone}"><td colspan="{ncols}">{text}</td></tr>')

    title = _STATEMENT_TITLE_MAP.get(model.get("statement"), "Statement")
    return (
        f'<div class="nb-card"><h3>{escape(title)} <small>· every account, nothing truncated</small></h3>'
        f'<div class="fs-scroll"><table class="fs-stmt num"><thead><tr>{"".join(headers)}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table></div></div>"
    )


def _fs_watch_html(watch: list[dict]) -> str:
    if not watch:
        return ""
    items = "".join(
        f'<span class="fs-witem"><span class="fs-dot {_WATCH_TONE_CLASSES.get(w.get("tone"), "fs-warn")}">'
        f"</span>{escape(str(w.get('text', '')))}</span>"
        for w in watch
    )
    return f'<div class="fs-watch">{items}</div>'


def _fs_highlights_html(highlights: list[str]) -> str:
    if not highlights:
        return ""
    items = "".join(f"<li>{escape(str(h))}</li>" for h in highlights)
    return (
        '<div class="nb-card"><h3>Highlights <small>· computed, materiality-gated</small></h3>'
        f'<ul class="fs-hl">{items}</ul></div>'
    )


def _fs_narrative_html(narrative: list[str]) -> str:
    if not narrative:
        return ""
    paras = "".join(f"<p>{escape(str(p))}</p>" for p in narrative)
    return f'<div class="nb-card fs-narr"><h3>Narrative <small>· every figure tool-computed</small></h3>{paras}</div>'


def _fs_meta_html(model: dict) -> str:
    chips = [f'<span class="fs-chip fs-dark">{escape(str(model.get("period", "")))}</span>']
    if model.get("prior_period"):
        chips.append(f'<span class="fs-chip">vs {escape(str(model["prior_period"]))}</span>')
    if model.get("yoy_period"):
        chips.append(f'<span class="fs-chip">vs {escape(str(model["yoy_period"]))} YoY</span>')
    for check in model.get("checks") or []:
        tone = "fs-good" if check.get("ok") else "fs-bad"
        mark = "✓" if check.get("ok") else "✗"
        chips.append(f'<span class="fs-chip {tone}">{mark} {escape(str(check.get("label", "")))}</span>')
    return f'<div class="fs-meta">{"".join(chips)}</div>'


def _financial_statement_html(model: dict) -> str:
    if not model:
        return ""
    kpis_html = "".join(_fs_kpi_html(k) for k in model.get("kpis") or [])
    trend_html = _fs_trend_html(model.get("trend"))
    quad_html = _fs_quad_html(model)
    # T2 gate minor[6]: BS/TB never have a trend chart (no trend source), so the mid-fold's
    # default 2-column grid left a dead empty column beside the quad — collapse to a single
    # column whenever only ONE of trend/quad is actually present.
    mid_cls = "fs-mid" if (trend_html and quad_html) else "fs-mid fs-mid--single"
    mid_html = f'<div class="{mid_cls}">{trend_html}{quad_html}</div>' if (trend_html or quad_html) else ""
    stmt_html = _fs_statement_table_html(model)
    hl_html = _fs_highlights_html(model.get("highlights") or [])
    narr_html = _fs_narrative_html(model.get("narrative") or [])
    twocol_html = f'<div class="fs-twocol">{hl_html}{narr_html}</div>' if (hl_html or narr_html) else ""
    return (
        f'<div class="fs">{_fs_meta_html(model)}{_fs_watch_html(model.get("watch") or [])}'
        f'<div class="fs-kpis">{kpis_html}</div>{mid_html}{stmt_html}{twocol_html}</div>'
    )


# ---------------------------------------------------------------------------
# Task 2 (Slice 1) — inventory_aging section renderers.
#
# Consumes ONLY `AgingReport` (Task 1, backend/app/services/report/inventory_aging.py)
# or a slice of it — mirrors the `financial_statement` section's "model" seam
# (a render-ready value already sitting on the section dict, see
# `_financial_statement_html`'s docstring above) rather than inventing a new contract.
# `build_inventory_aging_sections` below is that seam's producer: it turns a computed
# AgingReport into the section list `render_report_html` already knows how to join.
#
# Wiring THIS module's section types into a live compose/refresh path (calling
# `inventory_aging.compute()` on real tool payloads and feeding the result to
# `build_inventory_aging_sections`) is a LATER Slice-1 task, same as Task 1's own
# docstring says about `report_service.assemble_spec` — this only has to render
# whatever AgingReport it's given, correctly and matching the mock, which is what
# every test in `tests/report/test_inventory_aging_render.py` checks. The AgingReport
# object flows through `model` UNSERIALIZED (a live dataclass instance, not a JSON-safe
# dict) — a deliberate, documented simplification: a recipe-JSON round trip isn't this
# task's concern until that later wiring task exists.
#
# Unlike `financial_statement` (whose statement_builder pre-formats every displayed
# string), Task 1's `compute()` deliberately leaves KPI/table figures as raw `Decimal`
# (only WatchItem.text/Highlight.text/Narrative are pre-formatted prose) — so this
# module owns its OWN money/percent/points formatting, matching the mock's conventions
# (design rule #9): whole dollars with thousands grouping and parentheses for negative
# in TABLE cells (`_ia_money`/`_ia_pct`), abbreviated ($22.70M/$867.8K) in KPI cards
# (`_ia_abbrev_money`), and a signed typographic-minus form for point deltas
# (`_ia_pts`, reusing the `_MINUS` glyph the financial_statement renderer already
# defines above).
# ---------------------------------------------------------------------------

_IA_DOT_CLASS = {"red": "unf", "green": "fav", "amber": "warn", "grey": ""}

# One color per location, cycled if there are ever more than 5 (the mock's own palette:
# --b5 near-black, --accent orange, --b3 tan, then two more --b* steps). The MOCK
# dashes its third series (Panurgy) purely because a 3rd solid line on a light "b3" tan
# was hard to tell from the gridlines at a glance — every series from the third onward
# gets the same dashed treatment here, not just literally the third.
_IA_PALETTE: tuple[str, ...] = ("#2B2722", "#E0641F", "#AFA48C", "#6E6552", "#CFC7B4")
_IA_BUCKET_SWATCH = {
    "0-30": "var(--ia-b1)",
    "31-60": "var(--ia-b2)",
    "61-90": "var(--ia-b3)",
    "91-180": "var(--ia-b4)",
    "180+": "var(--ia-b5)",
}

_IA_SPARK_W, _IA_SPARK_H = 200.0, 34.0
_IA_TREND_W, _IA_TREND_H = 600.0, 232.0
_IA_TREND_PAD_L, _IA_TREND_PAD_R, _IA_TREND_PAD_T, _IA_TREND_PAD_B = 52.0, 60.0, 20.0, 26.0

_IA_TOP_HEADER = (
    '<tr><th>SKU</th><th class="desc" style="text-align:left">Item</th><th>Category</th>'
    # "Days" / "% of aged" (not the fuller "Days since restock" / "% of location
    # aged") -- render-fidelity fix: at the report's real in-app width (~780px) the
    # longer labels clipped the table; the Aging buckets section (rendered just
    # above this one) already establishes "days since last restock" as the page's
    # aging metric, so the short form loses no meaning here.
    "<th>Units</th><th>Value $</th><th>Days</th><th>% of aged</th></tr>"
)


def _ia_money(value: Decimal) -> str:
    """Whole-dollar TABLE-cell string: thousands-grouped, no decimals, parentheses for
    negative (design rule #9) — ``Decimal('1380661') -> '1,380,661'``,
    ``Decimal('-542687') -> '(542,687)'``."""
    q = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"({abs(q):,})" if q < 0 else f"{q:,}"


def _ia_pct(value: Decimal) -> str:
    """1dp percent, parentheses for negative — ``'7.3%'``, ``'(24.7%)'``."""
    return f"({abs(value)}%)" if value < 0 else f"{value}%"


def _ia_signed_pct(value: Decimal) -> str:
    sign = "+" if value >= 0 else _MINUS
    return f"{sign}{abs(value)}%"


def _ia_pts(value: Decimal) -> str:
    """Signed point delta, typographic minus (matches the mock's Δ pts column /
    KPI-card sub-line) — ``'+6.0'``, ``'−3.7'``."""
    sign = "+" if value >= 0 else _MINUS
    return f"{sign}{abs(value)}"


def _ia_abbrev_money(value: Decimal) -> str:
    """Abbreviated $ for KPI cards / bucket-table group headers (unsigned) —
    ``'$22.70M'`` / ``'$867.8K'`` / ``'$500'``."""
    v = abs(value)
    sign = "-" if value < 0 else ""
    if v >= Decimal("1000000"):
        return f"{sign}${(v / Decimal('1000000')):.2f}M"
    if v >= Decimal("1000"):
        return f"{sign}${(v / Decimal('1000')):.1f}K"
    return f"{sign}${v:,.0f}"


def _ia_signed_abbrev_money(value: Decimal) -> str:
    sign = "+" if value >= 0 else _MINUS
    return f"{sign}{_ia_abbrev_money(abs(value))}"


def _ia_dollar_money(value: Decimal) -> str:
    """PROSE money (not a table cell) -- ``_ia_money()`` with a literal '$' prefix.
    The mock's group-header prose reads e.g. "aged $3,230,556" (design rule #9's
    table-cell convention -- bare numbers, sign carried by the column header --
    does not apply to sentence text)."""
    return f"${_ia_money(value)}"


# Display labels for Task 1's bucket identifiers (inventory_aging.py's BUCKETS):
# the mock's binding copy uses an en dash (U+2013) throughout ("0–30 days"), not
# the ASCII hyphen the bucket codes themselves use ("0-30") -- those codes are a
# code-facing identifier, not display copy, so they're mapped rather than
# interpolated directly.
_IA_BUCKET_LABEL = {
    "0-30": "0–30",
    "31-60": "31–60",
    "61-90": "61–90",
    "91-180": "91–180",
    "180+": "180+",
}


def _ia_short_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def _ia_watch_html(items: list[dict]) -> str:
    if not items:
        return ""
    chips = "".join(
        f'<span class="ia-chip {_IA_DOT_CLASS.get(w["dot"], "")}"><span class="dot"></span>{escape(w["text"])}</span>'
        for w in items
    )
    return (
        '<div class="ia-section"><h2>Watch items '
        "<span>computed · materiality-gated at $50K or 1 pt of share</span></h2>"
        f'<div class="ia-watch">{chips}</div></div>'
    )


def _ia_sparkline_svg(values: list[str] | tuple[Decimal, ...]) -> str:
    """A KPI-card sparkline (mock: 200x34 viewBox, polyline + emphasised endpoint dot).
    ``None``/an empty/single-point series (e.g. the aged>180 card — Task 1's r_trend has
    no 180+ series, see KpiCard.sparkline's own docstring) renders nothing, same
    "needs >=2 points" rule as the financial_statement renderer's `_fs_sparkline_svg`.
    Gate fix #4: ``float(v)`` already accepts either a real ``Decimal`` or its
    decimal-literal string form identically — no ``Decimal(...)`` reconstruction is
    needed here, unlike the money/percent-formatting call sites elsewhere in this
    module that do arithmetic/``.quantize()``."""
    if not values or len(values) < 2:
        return ""
    floats = [float(v) for v in values]
    vmin, vmax = min(floats), max(floats)
    span = (vmax - vmin) or 1.0
    x0, x1, y0, y1 = 4.0, _IA_SPARK_W - 4.0, 4.0, _IA_SPARK_H - 4.0
    step = (x1 - x0) / (len(floats) - 1)
    pts = [(x0 + i * step, y1 - ((v - vmin) / span) * (y1 - y0)) for i, v in enumerate(floats)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    ex, ey = pts[-1]
    return (
        f'<svg viewBox="0 0 {_IA_SPARK_W:.0f} {_IA_SPARK_H:.0f}" preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{path}"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.5" fill="currentColor"/></svg>'
    )


def _ia_kpi_delta_html(kpi: dict) -> str:
    """Render-fidelity fix: the "vs prior week" / "vs {prior %}" label lives INSIDE
    the same `<b>` element as the delta, not as a sibling text node in the
    surrounding `.d` flex div — `.d` is `display: flex; flex-wrap: wrap`, and at the
    report's real width that was splitting the delta and its label onto two lines.
    One element means flexbox has nothing left to wrap between (see the matching
    `.ia-kpis .kpi .d` font-size step-down in `_IA_CSS`, just below this function).

    Gate fix #4: `kpi` is the JSON-safe dict form (decimal-literal strings) — every
    field this function does arithmetic/comparison on is reconstructed via
    ``Decimal(...)`` before use; a field only ever interpolated verbatim (never here)
    would not need it, since ``str(Decimal(s)) == s`` for a string ``json_safe``
    itself produced."""
    cls = "fav" if kpi["favourable"] else "unf"
    delta = Decimal(kpi["delta"])
    arrow = "▲" if delta >= 0 else "▼"
    if kpi["delta_pct"] is not None:
        main = f"{arrow} {_ia_signed_abbrev_money(delta)} · {_ia_signed_pct(Decimal(kpi['delta_pct']))} vs prior week"
        return f'<div class="d"><b class="{cls}">{main}</b></div>'
    # The aged-share card (KpiCard.delta_pct is None): the mock shows a point delta
    # plus "vs {prior %}" rather than "vs prior week" — reconstruct the prior share
    # from value/delta rather than adding a field Task 1's AgingReport doesn't carry.
    prior_value = Decimal(kpi["value"]) - delta
    main = f"{arrow} {_ia_pts(delta)} pts vs {_ia_pct(prior_value)}"
    return f'<div class="d"><b class="{cls}">{main}</b></div>'


def _ia_kpi_html(kpi: dict) -> str:
    value = Decimal(kpi["value"])
    value_str = f"{value}%" if kpi["key"] == "aged_share" else _ia_abbrev_money(value)
    spark = _ia_sparkline_svg(kpi["sparkline"])
    return (
        f'<div class="kpi"><div class="l">{escape(kpi["label"])}</div>'
        f'<div class="v tnum">{escape(value_str)}</div>{_ia_kpi_delta_html(kpi)}'
        f'<div class="s">{escape(kpi["sub_detail"])}</div>{spark}</div>'
    )


def _ia_kpi_cards_html(kpis: list[dict]) -> str:
    if not kpis:
        return ""
    return f'<div class="ia-kpis">{"".join(_ia_kpi_html(k) for k in kpis)}</div>'


def _ia_trend_chart_html(report: dict) -> str:
    """One polyline per location (spec §A1 "trend chart ... line, aged share % per
    location, same scale, endpoint labels, legend"). Assumes every location's trend
    series shares the same weekly dates (true for any real recipe — one `r_trend`
    query, same `trend_weeks` param, per §A7) — the x-axis is read off whichever
    location has the most points so a genuinely ragged fixture still renders (never
    crashes), even though its labels then only line up exactly for the aligned
    locations.

    Gate fix #4: ``report`` is the JSON-safe dict form of ``AgingReport`` — a
    ``TrendPoint``'s ``d`` is an ISO date string (parsed back via
    ``date.fromisoformat`` for ``_ia_short_date``) and ``pct_90p`` a decimal-literal
    string (``float()`` accepts it directly, same as a real ``Decimal``)."""
    locations = [loc["location"] for loc in report["locations"]]
    series = [(loc, report["trend"].get(loc, ())) for loc in locations]
    axis_source = max((pts for _loc, pts in series), key=len, default=())
    if len(axis_source) < 2:
        return ""
    date_labels = [_ia_short_date(date.fromisoformat(tp["d"])) for tp in axis_source]

    plot_w = _IA_TREND_W - _IA_TREND_PAD_L - _IA_TREND_PAD_R
    plot_h = _IA_TREND_H - _IA_TREND_PAD_T - _IA_TREND_PAD_B
    y_top, y_bottom = _IA_TREND_PAD_T, _IA_TREND_PAD_T + plot_h
    all_pcts = [float(tp["pct_90p"]) for _loc, pts in series for tp in pts]
    raw_max = max([*all_pcts, 0.0])
    axis_max = max(10.0, math.ceil((raw_max or 1.0) / 5.0) * 5.0)

    def y_of(pct: float) -> float:
        return y_bottom - (pct / axis_max) * plot_h

    grid_parts = []
    for i in range(5):
        frac = i / 4
        y = y_top + frac * plot_h
        label_val = axis_max * (1 - frac)
        grid_parts.append(
            f'<line x1="{_IA_TREND_PAD_L:.0f}" y1="{y:.1f}" x2="{_IA_TREND_W - _IA_TREND_PAD_R:.0f}" y2="{y:.1f}"/>'
        )
        grid_parts.append(
            f'<text x="{_IA_TREND_PAD_L - 6:.0f}" y="{y + 4:.1f}" text-anchor="end">{label_val:.1f}%</text>'
        )

    x_label_parts = []
    x_step = plot_w / max(len(axis_source) - 1, 1)
    for i, label in enumerate(date_labels):
        x = _IA_TREND_PAD_L + i * x_step
        x_label_parts.append(f'<text x="{x:.1f}" y="{_IA_TREND_H - 6:.0f}" text-anchor="middle">{escape(label)}</text>')

    series_parts: list[str] = []
    endpoint_parts: list[str] = []
    label_parts: list[str] = []
    legend_items: list[str] = []
    for i, (loc_name, pts) in enumerate(series):
        if len(pts) < 2:
            continue
        color = _IA_PALETTE[i % len(_IA_PALETTE)]
        dashed = i >= 2
        step = plot_w / max(len(pts) - 1, 1)
        coords = [(_IA_TREND_PAD_L + j * step, y_of(float(tp["pct_90p"]))) for j, tp in enumerate(pts)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        dash_attr = ' stroke-dasharray="6 4"' if dashed else ""
        series_parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.5"{dash_attr} points="{path}"/>')
        ex, ey = coords[-1]
        last = pts[-1]
        tip = f"{loc_name} {_ia_short_date(date.fromisoformat(last['d']))}: {last['pct_90p']}%"
        endpoint_parts.append(
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{color}"><title>{escape(tip)}</title></circle>'
        )
        label_parts.append(
            f'<text x="{ex + 6:.1f}" y="{ey + 4:.1f}" fill="{color}" font-weight="700">{last["pct_90p"]}%</text>'
        )
        dash_note = " (dashed)" if dashed else ""
        legend_items.append(f'<span><i style="background:{color}"></i>{escape(loc_name)}{dash_note}</span>')

    svg = (
        f'<svg viewBox="0 0 {_IA_TREND_W:.0f} {_IA_TREND_H:.0f}" role="img" '
        'aria-label="Line chart: percent of on-hand value older than 90 days per location over time">'
        f'<g class="ia-grid">{"".join(grid_parts)}</g>'
        f"{''.join(series_parts)}<g>{''.join(endpoint_parts)}</g>"
        f'<g font-size="11" font-weight="700">{"".join(label_parts)}</g>'
        f'<g class="ia-xaxis">{"".join(x_label_parts)}</g></svg>'
    )
    legend = (
        f'<div class="legend">{"".join(legend_items)}'
        '<span class="muted">Same scale for every location · endpoint = this snapshot</span></div>'
    )
    span_txt = f"{date_labels[0]} – {date_labels[-1]}" if date_labels else ""
    return (
        '<div class="chart"><h3>Aged share of on-hand value, by location '
        f"<span>· % of value older than 90 days · weekly, {escape(span_txt)}</span></h3>"
        f"{svg}{legend}</div>"
    )


def _ia_variance_row_html(loc: dict, row_cls: str = "") -> str:
    delta_value = Decimal(loc["delta_value"])
    aged90_share_delta_pts = Decimal(loc["aged90_share_delta_pts"])
    delta_cls = "fav" if delta_value >= 0 else "unf"
    pts_cls = "fav" if aged90_share_delta_pts <= 0 else "unf"
    cells = (
        f'<td class="lbl">{escape(loc["location"])}</td>'
        f"<td>{_ia_money(Decimal(loc['on_hand_value']))}</td>"
        f"<td>{_ia_money(Decimal(loc['prior_value']))}</td>"
        f'<td class="{delta_cls}">{_ia_money(delta_value)}</td>'
        f'<td class="{delta_cls}">{_ia_pct(Decimal(loc["delta_pct"]))}</td>'
        f"<td>{_ia_money(Decimal(loc['aged90_value']))}</td>"
        f"<td>{loc['aged90_share_pct']}%</td>"
        f'<td class="{pts_cls}">{_ia_pts(aged90_share_delta_pts)}</td>'
    )
    cls_attr = f' class="{row_cls}"' if row_cls else ""
    return f"<tr{cls_attr}>{cells}</tr>"


def _ia_variance_table_html(report: dict) -> str:
    rows = "".join(_ia_variance_row_html(loc) for loc in report["locations"])
    rows += _ia_variance_row_html(report["all_locations"], "total")
    return (
        '<div class="tblcard"><h3>By location <span>· this week vs prior week</span></h3>'
        '<table class="tnum"><thead><tr><th>Location</th><th>On-hand $</th><th>Prior wk</th>'
        "<th>Δ $</th><th>Δ %</th><th>Aged &gt;90 $</th><th>Share</th><th>Δ pts</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        '<div class="muted">Share = aged &gt;90-day value ÷ on-hand value. Green/red mark '
        "favourable/unfavourable movement only; a lower aged share is favourable.</div></div>"
    )


def _ia_mid_row_html(report: dict) -> str:
    """The mock's 2-column ``.mid`` layout (review finding — major): the trend chart
    and the "By location" variance table render side-by-side, not as two independent
    full-width stacked cards. ``.ia-mid`` collapses to a single column under 900px,
    same responsive rule as the mock's own ``.mid`` (and the same pattern
    ``_FS_CSS``'s ``.fs-mid`` already uses for the financial_statement renderer)."""
    chart_html = _ia_trend_chart_html(report)
    variance_html = _ia_variance_table_html(report)
    if not chart_html and not variance_html:
        return ""
    return f'<div class="ia-section"><div class="ia-mid">{chart_html}{variance_html}</div></div>'


def _ia_bucket_row_html(bucket: str, locations: list[dict]) -> str:
    swatch = f'<span class="bar" style="background:{_IA_BUCKET_SWATCH[bucket]}"></span>'
    cells = f'<td class="lbl">{swatch}{_IA_BUCKET_LABEL[bucket]} days</td>'
    for loc in locations:
        br = next(b for b in loc["buckets"] if b["bucket"] == bucket)
        cells += (
            f'<td>{_ia_money(Decimal(br["value"]))}</td><td class="pct">{br["pct_of_location"]}%</td>'
            f"<td>{br['units']:,}</td><td>{br['skus']}</td>"
        )
    return f"<tr>{cells}</tr>"


def _ia_bucket_subtotal_row_html(label: str, row_cls: str, locations: list[dict], values_fn) -> str:
    cells = f'<td class="lbl">{label}</td>'
    for loc in locations:
        value, pct, units, skus = values_fn(loc)
        cells += f'<td>{_ia_money(value)}</td><td class="pct">{pct}%</td><td>{units:,}</td><td>{skus}</td>'
    return f'<tr class="{row_cls}">{cells}</tr>'


def _ia_bucket_table_html(report: dict) -> str:
    locations = report["locations"]
    if not locations:
        return ""
    header = "<th>Bucket</th>" + "<th>Value $</th><th>% of location</th><th>Units</th><th>SKUs</th>" * len(locations)
    group = '<td class="lbl"></td>' + "".join(
        f'<td colspan="4" style="text-align:center">{escape(loc["location"])} · '
        f"{_ia_abbrev_money(Decimal(loc['on_hand_value']))}</td>"
        for loc in locations
    )
    rows = [f'<tr class="group">{group}</tr>']
    for bucket in ("0-30", "31-60", "61-90"):
        rows.append(_ia_bucket_row_html(bucket, locations))
    rows.append(
        _ia_bucket_subtotal_row_html(
            "Current (≤ 90 days)",
            "sub",
            locations,
            lambda loc: (
                Decimal(loc["on_hand_value"]) - Decimal(loc["aged90_value"]),
                share_pct(Decimal(loc["on_hand_value"]) - Decimal(loc["aged90_value"]), Decimal(loc["on_hand_value"])),
                loc["units"] - loc["aged90_units"],
                loc["skus"] - loc["aged90_skus"],
            ),
        )
    )
    for bucket in ("91-180", "180+"):
        rows.append(_ia_bucket_row_html(bucket, locations))
    rows.append(
        _ia_bucket_subtotal_row_html(
            "Aged (&gt; 90 days)",
            "sub",
            locations,
            lambda loc: (
                Decimal(loc["aged90_value"]),
                loc["aged90_share_pct"],
                loc["aged90_units"],
                loc["aged90_skus"],
            ),
        )
    )
    rows.append(
        _ia_bucket_subtotal_row_html(
            "On hand",
            "total",
            locations,
            lambda loc: (Decimal(loc["on_hand_value"]), Decimal("100"), loc["units"], loc["skus"]),
        )
    )
    return (
        '<div class="ia-section"><h2>Aging buckets by location '
        "<span>· days since last restock · value, units, SKUs · nothing truncated</span></h2>"
        f'<div class="tblcard"><table class="tnum"><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _ia_top_item_row_html(it: dict, loc: dict) -> str:
    pct = share_pct(Decimal(it["value"]), Decimal(loc["aged90_value"]))
    return (
        f'<tr><td class="lbl mono">{escape(it["sku"])}</td><td class="desc">{escape(it["item_desc"])}</td>'
        f"<td>{escape(it['category'])}</td><td>{it['units']:,}</td><td>{_ia_money(Decimal(it['value']))}</td>"
        f'<td>{it["days"]}</td><td class="pct">{pct}%</td></tr>'
    )


def _ia_top_positions_html(report: dict) -> str:
    """The top-5 table draws from ``top_items`` (capped); the collapsible "All N"
    details block draws from ``aged_items`` — the UNBOUNDED per-location list — so
    N is always the TRUE aged-SKU count and the details block is never byte-
    identical to the top-5 table above it when a location has more than 5 aged
    SKUs (review finding: this used to source both from ``top_items``, which
    made "All N aged SKUs" false-complete for any location with > 5 aged items)."""
    top_rows: list[str] = []
    all_rows: list[str] = []
    total_aged = 0
    for loc in report["locations"]:
        items = report["top_items"].get(loc["location"], ())
        top_rows.append(
            f'<tr class="group"><td class="lbl" colspan="7">{escape(loc["location"])} · aged '
            f"{_ia_dollar_money(Decimal(loc['aged90_value']))} · top 5 = {loc['top5_share_pct']}%</td></tr>"
        )
        for it in items:
            top_rows.append(_ia_top_item_row_html(it, loc))

    for loc in report["locations"]:
        aged_items = report["aged_items"].get(loc["location"], ())
        total_aged += len(aged_items)
        all_rows.append(
            f'<tr class="group"><td class="lbl" colspan="7">{escape(loc["location"])} · aged '
            f"{_ia_dollar_money(Decimal(loc['aged90_value']))} · {len(aged_items)} SKUs</td></tr>"
        )
        for it in aged_items:
            all_rows.append(_ia_top_item_row_html(it, loc))

    details = (
        f"<details><summary>All {total_aged} aged SKUs (collapsed here; the Excel file "
        "carries every SKU)</summary>"
        f'<div class="tblcard"><table class="tnum"><thead>{_IA_TOP_HEADER}</thead>'
        f"<tbody>{''.join(all_rows)}</tbody></table></div></details>"
    )
    return (
        '<div class="ia-section"><h2>Largest aged positions '
        "<span>· top 5 per location by value, older than 90 days · the Excel file "
        "carries every SKU</span></h2>"
        f'<div class="tblcard"><table class="tnum"><thead>{_IA_TOP_HEADER}</thead>'
        f"<tbody>{''.join(top_rows)}</tbody></table>{details}</div></div>"
    )


def _ia_highlights_html(highlights: list[dict]) -> str:
    if not highlights:
        return ""
    items = "".join(f"<li>{escape(h['text'])}</li>" for h in highlights)
    return (
        '<div class="ia-section"><h2>Highlights '
        "<span>· driver attribution, largest movers first, threshold-gated</span></h2>"
        f'<ul class="hl">{items}</ul></div>'
    )


def _ia_narrative_html(narrative: dict) -> str:
    return (
        '<div class="ia-section"><h2>Narrative '
        "<span>· deterministic template · every figure tool-computed</span></h2>"
        f'<div class="narr"><p>{escape(narrative["paragraph_1"])}</p>'
        f"<p>{escape(narrative['paragraph_2'])}</p></div></div>"
    )


def build_inventory_aging_provenance(prov: Provenance) -> list[dict]:
    """Provenance entries for inventory_aging's "Sources & method" block (spec §A1) —
    reuses the EXISTING generic `_provenance_html` renderer (its `<strong>Sources &amp;
    method</strong>` heading already matches the mock verbatim) rather than a bespoke
    grid, per "reuse existing renderers where they exist". `executed_at`/`bytes_scanned`
    are `None` on a pure `compute()` result (Task 1's own docstring: filled in by the
    live compose/refresh wiring, a later task) — rendered as an honestly-empty stamp
    rather than a fabricated one."""
    executed_at = prov.executed_at or ""
    entries = [
        {
            "result_id": "r_items",
            "label": "BigQuery inventory snapshot",
            "detail": f"{prov.source} · {prov.age_definition}",
            "executed_at": executed_at,
            "resolved": True,
        }
    ]
    snaps = ", ".join(
        f"{loc}: {first.isoformat()}–{last.isoformat()} ({count} snapshots)"
        for loc, (first, last, count) in sorted(prov.snapshots_used.items())
    )
    if snaps:
        entries.append(
            {
                "result_id": "r_meta",
                "label": "Snapshots used",
                "detail": snaps,
                "executed_at": executed_at,
                "resolved": True,
            }
        )
    entries.append(
        {
            "result_id": "r_prior/r_trend",
            "label": f"{prov.query_count} bigquery_sql queries",
            "detail": "; ".join(prov.integrity_checks),
            "executed_at": executed_at,
            "resolved": True,
        }
    )
    return entries


def _ia_bytes_human(n: int) -> str:
    """Human byte count for the "Queries" row's "... scanned" clause (mock:
    ``1.4 GB``) — same unit-stepping shape as ``_ia_abbrev_money``, base 1024."""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"  # pragma: no cover — no realistic single-query scan reaches PB


def _ia_provenance_html(prov: dict) -> str:
    """The mock's labelled "Sources & method" grid (render-polish brief item 6):
    rows Source / Snapshots used / Age (full width) / Queries / Integrity, each a
    bold-labelled ``<div>`` cell, rendered inside THIS module's own ``.ia-section``
    styling (the ``.ia-prov`` grid) — never the shared ``_provenance_html`` plain-
    footer renderer every OTHER report type still uses unmodified. Reads the SAME
    ``Provenance`` fields Task 1's ``compute()`` already produces (source /
    snapshots_used / age_definition / query_count / executed_at / bytes_scanned /
    integrity_checks) — no new data invented for this row layout, per the brief's
    "keep the existing provenance data behind it".

    Gate fix #4: ``prov`` is the JSON-safe dict form — ``snapshots_used``'s values
    are already ISO date STRINGS (json_safe's tuple branch), so they're used
    verbatim rather than re-parsed through ``date.fromisoformat``."""
    table = escape(prov["source"].strip("`"))
    snaps = "; ".join(
        f"{escape(loc)}: {first}–{last} ({count} snapshots)"
        for loc, (first, last, count) in sorted(prov["snapshots_used"].items())
    )
    age = escape(prov["age_definition"])
    queries = f'{prov["query_count"]} · <span class="mono">bigquery_sql</span>'
    if prov["executed_at"]:
        queries += f" · executed {_fmt_stamp(prov['executed_at'])}"
    if prov["bytes_scanned"] is not None:
        queries += f" · {escape(_ia_bytes_human(prov['bytes_scanned']))} scanned"
    # Each entry in `integrity_checks` already carries its own terminal period (see
    # INTEGRITY_CHECKS in inventory_aging.py) -- join with a single space, not "; ",
    # or the trailing period + separator collide into a stray ".;" (render-fidelity
    # fix: the joined cell used to read "...on-hand value.; The all-locations row...
    # re-query.; no model generated a figure.").
    integrity = escape(" ".join(prov["integrity_checks"]))
    integrity = f"{integrity} no model generated a figure." if integrity else "No model generated a figure."
    rows = (
        f'<div><b>Source</b> BigQuery <span class="mono">{table}</span></div>'
        f"<div><b>Snapshots used</b> {snaps}</div>"
        f'<div class="full"><b>Age</b> {age}</div>'
        f"<div><b>Queries</b> {queries}</div>"
        f"<div><b>Integrity</b> {integrity}</div>"
    )
    return f'<div class="ia-section"><h2>Sources &amp; method</h2><div class="ia-prov">{rows}</div></div>'


# The set of section `type`s this module owns in `_section_html` — used by
# `render_report_html` to decide whether `_IA_CSS` needs to ship (same additive +
# conditional pattern as `_FS_CSS`/`has_financial_statement` above: a report with no
# inventory_aging section pays nothing for this CSS, and stays byte-identical to before
# this task).
_IA_SECTION_TYPES = frozenset(
    {
        "report_head",
        "watch_items",
        "kpi_cards",
        "trend_chart",
        "variance_table",
        "mid_row",
        "bucket_table",
        "top_positions",
        "highlights",
        "provenance_grid",
    }
)


def _ia_long_date(d: date) -> str:
    """The mock's head date form: ``8 Sep 2026`` (day, abbreviated month, year)."""
    return f"{d.day} {d.strftime('%b %Y')}"


def inventory_aging_title(report: AgingReport) -> str:
    """The page's <h1>, verbatim from the mock: ``Inventory Aging — Week of 8 Sep 2026``
    (spec §A1). Distinct from the Report ROW's series title "Inventory Aging Weekly"
    (§A6) — the row title names the series in the app's page header and the Drive
    folder; the <h1> names the week."""
    return f"Inventory Aging — Week of {_ia_long_date(report.snapshot_date)}"


def build_inventory_aging_head(report: AgingReport, *, composed_at: str | None = None) -> dict:
    """The mock's ``.report-head`` sub-line + meta block as a JSON-native model (plain
    strings only: spec_json is persisted as JSONB and re-rendered from it by
    scripts/backfill_report_html.py, so the model must survive a JSON round trip
    byte-for-byte). ``composed_at`` is the compose timestamp in ISO form (the compose
    script's ``now``); None omits the "Composed …" fragment rather than inventing one.
    The mock's tenant-name / "weekly, Monday 06:00 PT" line is the schedule's — Part B —
    and is deliberately not rendered until a schedule actually owns this report."""
    locations = " · ".join(loc.location for loc in report.locations)
    sub = (
        f"{locations} · on-hand stock aged by days since last restock · compared with "
        f"{_ia_long_date(report.prior_date)}"
    )
    return {
        "sub": sub,
        "snapshot": report.snapshot_date.isoformat(),
        "prior": report.prior_date.isoformat(),
        "composed_at": composed_at,
    }


def _ia_report_head_html(model: dict) -> str:
    sub = escape(str(model.get("sub", "")))
    snapshot = escape(str(model.get("snapshot", "")))
    prior = escape(str(model.get("prior", "")))
    composed_at = model.get("composed_at")
    composed = f"Composed {_fmt_stamp(composed_at)} · " if composed_at else ""
    return (
        '<div class="ia-head">'
        f'<div class="ia-sub">{sub}</div>'
        f'<div class="ia-meta">Snapshot <b>{snapshot}</b> · prior <b>{prior}</b><br>'
        f"{composed}no model generated a figure</div>"
        "</div>"
    )


def build_inventory_aging_sections(report: AgingReport, *, composed_at: str | None = None) -> list[dict]:
    """Turn a computed `AgingReport` (Task 1) into the section list `render_report_html`
    already knows how to join — one dict per section type, `model` holding exactly the
    slice of `report` that type's renderer needs (see the `_section_html` branches
    below). The `narrative` type deliberately reuses the EXISTING generic "narrative"
    section (markdown -> `_md_block`) for the two paragraphs — a `model` key on a
    narrative section (this function's own inventory_aging narrative) is what
    distinguishes it from a plain `markdown` one; `_section_html` branches on which key
    is present, so every existing `{"type": "narrative", "markdown": ...}` caller is
    completely unaffected (see `_section_html`'s comment there).

    ``mid_row`` (review finding — major) replaces what used to be two independent
    `trend_chart` + `variance_table` sections: the mock renders those two cards
    side-by-side in one 2-column row (`.mid`), not as separate full-width stacked
    cards, so `_ia_mid_row_html` renders both from a single `model` (the whole
    report — each card only needs its own slice, same as the standalone
    `trend_chart`/`variance_table` types those two renderer functions still serve).

    ``provenance_grid`` (render-polish brief item 6) is the mock's labelled
    "Sources & method" grid, appended LAST (matching the mock's own placement,
    after Narrative) — it renders from `report.provenance` directly via
    `_ia_provenance_html`, so `render_report_html` suppresses its OWN generic
    top-level `provenance=` footer for any spec whose sections include an
    inventory_aging block (see `has_inventory_aging` there): the two would
    otherwise render the SAME heading twice for the production compose/refresh
    call sites, which still build and pass `build_inventory_aging_provenance`'s
    list unchanged.

    Gate fix #4 (one representation for the rendered model): every `_ia_*_html`
    renderer reads dict form ONLY — this is the SINGLE conversion point
    (``inventory_aging.json_safe``, called exactly once, here) that turns the live
    `AgingReport` dataclass tree into the same JSON-safe dict shape a report's
    persisted `spec_json` already carries (see `report_service.spec_json_safe`,
    which applies the identical conversion before a compose/refresh commits). A
    caller that instead re-renders straight off an already-persisted, JSON-round-
    tripped `spec_json` (e.g. `scripts/backfill_report_html.py`) therefore produces
    BYTE-IDENTICAL HTML to this fresh-compute path — there is no second "the
    renderers expect a live dataclass" representation left to disagree with it.
    `build_inventory_aging_head` is the one exception: it already returns a plain
    JSON-safe dict (see its own docstring), so it's still called against the live
    `report` directly rather than the converted copy — redundant, not wrong."""
    from app.services.report.inventory_aging import json_safe as ia_json_safe

    report_dict = ia_json_safe(report)
    return [
        {"type": "report_head", "model": build_inventory_aging_head(report, composed_at=composed_at)},
        {"type": "watch_items", "model": report_dict["watch_items"]},
        {"type": "kpi_cards", "model": report_dict["kpis"]},
        {"type": "mid_row", "model": report_dict},
        {"type": "bucket_table", "model": report_dict},
        {"type": "top_positions", "model": report_dict},
        {"type": "highlights", "model": report_dict["highlights"]},
        {"type": "narrative", "model": report_dict["narrative"]},
        {"type": "provenance_grid", "model": report_dict["provenance"]},
    ]


def render_report_html(
    spec: dict,
    accent_hsl: str = "240 6% 10%",
    freshness: dict | None = None,
    provenance: list[dict] | None = None,
) -> str:
    title = escape(str(spec.get("title", "Report")))
    body = "".join(_section_html(s) for s in spec.get("sections", []))
    prov = spec.get("provenance", {}) or {}
    sources = prov.get("sources", [])
    prov_html = ""
    if sources:
        items = "".join(f"<li>{escape(str(x))}</li>" for x in sources)
        prov_html = f'<div class="prov"><strong>Sources &amp; definitions</strong><ul>{items}</ul></div>'
    # Freshness stamp (Slice B refresh honesty, spec §4B): prose is compose-time text
    # while {{result:…}} placeholders re-resolve on refresh — the stamp discloses the
    # two vintages. None (the compose path) keeps the output byte-identical.
    stamp_html = ""
    if freshness:
        # Compose (playbook compose, first version) has no refreshed_at yet — omit any
        # component whose value is empty/falsy rather than joining a dangling "· Data
        # refreshed " with nothing after it.
        parts = []
        if freshness.get("composed_at"):
            parts.append(f"Narrative composed {_fmt_stamp(freshness['composed_at'])}")
        if freshness.get("refreshed_at"):
            parts.append(f"Data refreshed {_fmt_stamp(freshness['refreshed_at'])}")
        if parts:
            stamp_html = f'<div class="stamp">{" · ".join(parts)}</div>'
    # Task 2 (Slice 1) — a report with no inventory_aging section pays nothing for
    # _IA_CSS. The bespoke inventory_aging "narrative" section (see _section_html)
    # reuses the SHARED "narrative" type name, so it's detected by `model` presence
    # rather than by type alone (a plain markdown narrative section must never pull
    # this CSS in). Computed BEFORE `method_html` (below) because that gate needs it
    # too — see the comment there.
    has_inventory_aging = any(
        sec.get("type") in _IA_SECTION_TYPES or (sec.get("type") == "narrative" and "model" in sec)
        for sec in spec.get("sections", [])
    )
    # Render-polish brief item 6: an inventory_aging spec's OWN "Sources & method"
    # renders from its `provenance_grid` section (build_inventory_aging_sections
    # always appends one) — never ALSO from this generic top-level `provenance=`
    # footer, or the heading would render twice. The production compose/refresh
    # call sites still build and pass `build_inventory_aging_provenance`'s list
    # unchanged (out of this brief's scope to touch); this gate is what keeps that
    # a no-op instead of a duplicate block, with zero changes at those call sites.
    method_html = "" if has_inventory_aging else (_provenance_html(provenance) if provenance else "")
    css = _CSS % {"accent": escape(accent_hsl), "accent_ink": _accent_ink(accent_hsl)}
    # Additive + conditional: only reports that actually use a financial_statement
    # section pay for its CSS — see _FS_CSS's docstring-comment for why this must stay a
    # plain string append (not folded into the %-formatted _CSS) for byte-stability.
    has_financial_statement = any(sec.get("type") == "financial_statement" for sec in spec.get("sections", []))
    if has_financial_statement:
        css += _FS_CSS
    if has_inventory_aging:
        css += _IA_CSS
    # EYEBALL-GATE FIX (F1, round 3): statement pages get a wider canvas (.report--wide,
    # a MODIFIER class, never a change to the shared .report default) — see the .fs-mid
    # comment in _FS_CSS. Scoped to specs that actually carry a financial_statement
    # section, same gate as the CSS itself, so every other report type's root <div>
    # (and therefore its exact rendered bytes) is untouched.
    report_cls = "report report--wide" if has_financial_statement else "report"
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>{css}</style></head><body><div class="{report_cls}">'
        f'<div class="accent-bar"></div><h1>{title}</h1>{body}{method_html}{stamp_html}{prov_html}</div></body></html>'
    )
