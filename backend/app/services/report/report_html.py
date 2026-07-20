from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from html import escape

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
# financial_statement section is actually present) and every literal `%` below (there are
# none currently) would be safe verbatim either way.
#
# Reuses the base stylesheet's vars/idioms (--accent, --border, .nb-card box) and the
# generic table/th/td + td.num,th.num rules (money cells just get class="num" like every
# other table in this renderer) — only the differentiators called out in the design rule
# get bespoke classes here: KPI cards, chips/dots, quad+statement row emphasis, the
# CSS-only section-collapse, the trend chart legend, and print.
_FS_CSS = """
:root { --fs-good:#0A7A3D; --fs-bad:#B3261E; --fs-warn:#E8A13C; --fs-soft:#EFEDE7; }
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

.fs-mid { display:grid; grid-template-columns:1.5fr 1fr; gap:18px; margin-bottom:22px; }
@media (max-width:900px) { .fs-mid { grid-template-columns:1fr; } }
.fs-scroll { overflow-x:auto; }
.fs-legend { display:flex; gap:16px; flex-wrap:wrap; font-size:12px; font-weight:600; margin-top:8px; }
.fs-sw { width:12px; height:12px; border:2px solid var(--border); display:inline-block;
  margin-right:5px; vertical-align:-1px; }

/* Variance quad (design rule #5): Actual | Prior | Delta $ | Delta %% — reuses the same
   fs-sub/fs-formula/fs-net emphasis classes as the statement table below (both are built
   from the same _quad_row-shaped model dict). */
table.fs-quad th, table.fs-quad td { border:none; border-bottom:1px solid #ddd; font-size:12.5px; }
table.fs-quad th:first-child, table.fs-quad td:first-child { white-space:nowrap; }
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
        return f'<div class="nb-card svg-wrap">{_md_block(str(s.get("markdown", "")))}</div>'
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


def build_provenance(sources: dict, executed_at: str) -> list[dict]:
    """Translate a recipe's raw ``sources`` map (``result_id -> {tool, params, ...}``)
    into human-readable entries for the renderer's "Sources & method" block: each result
    id, a plain-English label for the tool that produced it, its params as ``detail``,
    and when it ran. Sorted by ``result_id`` for deterministic (byte-stable) output.

    ``detail`` is policy-filtered (see ``_ALWAYS_EXCLUDED_PARAM_KEYS`` / ``_LLM_ONLY_PARAMS``
    above) and each surviving value length-capped (``_DETAIL_VALUE_MAX_LEN``) — never a raw
    dump of every captured param. Playbook sources (``report_type``, ``period``) and
    external-MCP params (e.g. ``reportId``) are unaffected."""
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
        entries.append({"result_id": result_id, "label": label, "detail": detail, "executed_at": executed_at})
    return entries


def _provenance_html(provenance: list[dict]) -> str:
    rows = "".join(
        f"<div>{escape(str(p.get('result_id', '')))} — {escape(str(p.get('label', '')))}"
        f" · {escape(str(p.get('detail', '')))} · executed {escape(str(p.get('executed_at', '')))}</div>"
        for p in provenance
    )
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
_FS_TREND_PAD_L, _FS_TREND_PAD_R, _FS_TREND_PAD_T, _FS_TREND_PAD_B = 56, 16, 24, 34


def _fs_sign_tone(text: str) -> tuple[str, str]:
    """(tone_class, arrow) from an already-FORMATTED delta string's own sign — used for
    KPI headline cards, all of which are inherently "higher is better" metrics (revenue,
    gross/operating/net income for IS; assets/liabilities/equity, debits/credits for
    BS/TB, shown as a simplifying default since a balance-sheet line has no P&L
    favorability framing). ``""`` tone = neutral (a zero delta gets neither color)."""
    if text in ("$0", "0.0%", "0.0pp"):
        return "", "•"
    if text.startswith(_MINUS):
        return "fs-bad", "▼"
    return "fs-good", "▲"


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
        tone, arrow = _fs_sign_tone(mom_text)
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
    all_vals = [float(v) for s in series for v in (s.get("values") or [])]
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
    for s in series:
        values = s.get("values") or []
        if len(values) != n:
            continue  # a malformed/partial series is skipped rather than mis-plotted
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
    prior/delta/reduces_profit). These never carry ``pct_rev`` (only individual account
    rows do) — the "% of rev" cell, when that column exists, is left BLANK for a summary
    row rather than invented here (the renderer never computes a figure the model didn't
    supply)."""
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
        cells.append('<td class="num fs-pct"></td>')
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
        # Δ%% needs its own cell (the shared _fs_summary_row_html helper only emits
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
        f'<table class="fs-quad num"><thead><tr>{"".join(headers)}</tr></thead><tbody>{rows}</tbody></table></div>'
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

    body_rows: list[str] = []
    for sec_idx, sec in enumerate(sections):
        body_rows.append(_fs_section_header_html(sec.get("label", ""), sec_idx, ncols))
        for acct in sec.get("accounts", []):
            body_rows.append(_fs_account_row_html(acct, sec_idx, has_prior=has_prior, has_pct_rev=has_pct_rev))
        body_rows.append(_fs_summary_row_html(sec["subtotal"], "fs-sub", has_prior=has_prior, has_pct_rev=has_pct_rev))

    for formula_row in model.get("formulas") or []:
        body_rows.append(_fs_summary_row_html(formula_row, "fs-formula", has_prior=has_prior, has_pct_rev=has_pct_rev))
    if model.get("net") is not None:
        body_rows.append(_fs_summary_row_html(model["net"], "fs-net", has_prior=has_prior, has_pct_rev=has_pct_rev))
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
    mid_html = f'<div class="fs-mid">{trend_html}{quad_html}</div>' if (trend_html or quad_html) else ""
    stmt_html = _fs_statement_table_html(model)
    hl_html = _fs_highlights_html(model.get("highlights") or [])
    narr_html = _fs_narrative_html(model.get("narrative") or [])
    twocol_html = f'<div class="fs-twocol">{hl_html}{narr_html}</div>' if (hl_html or narr_html) else ""
    return (
        f'<div class="fs">{_fs_meta_html(model)}{_fs_watch_html(model.get("watch") or [])}'
        f'<div class="fs-kpis">{kpis_html}</div>{mid_html}{stmt_html}{twocol_html}</div>'
    )


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
    method_html = _provenance_html(provenance) if provenance else ""
    css = _CSS % {"accent": escape(accent_hsl), "accent_ink": _accent_ink(accent_hsl)}
    # Additive + conditional: only reports that actually use a financial_statement
    # section pay for its CSS — see _FS_CSS's docstring-comment for why this must stay a
    # plain string append (not folded into the %-formatted _CSS) for byte-stability.
    if any(sec.get("type") == "financial_statement" for sec in spec.get("sections", [])):
        css += _FS_CSS
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>{css}</style></head><body><div class="report">'
        f'<div class="accent-bar"></div><h1>{title}</h1>{body}{method_html}{stamp_html}{prov_html}</div></body></html>'
    )
