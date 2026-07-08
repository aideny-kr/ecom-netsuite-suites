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
:root { --bg:#FAF9F6; --ink:#111; --border:#000; --card:#FFF; --accent:hsl(%(accent)s); }
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
th { background:var(--accent); font-weight:800; }
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
  thead th { position:static; }
  .chart-legend input { display:none; }
  .report { max-width:100%%; padding:0; }
}
"""

# Slice D — CSS-only series toggles: unchecking the legend's ser-j checkbox hides that
# series' <g class="ser-j"> groups. :has() is load-bearing (CSS has no parent
# combinator; the alternative is id/for label pairs, which collide across a report's
# multiple charts); browsers without :has() degrade to inert checkboxes. Static block
# for j=0..11 — matches the 12-category legibility cap; contains no '%' so it is safe
# through _CSS's %-formatting.
_CSS += "".join(
    f".nb-card:has(input.ser-{j}:not(:checked)) svg .ser-{j} {{ display:none; }}\n" for j in range(12)
)


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


def render_report_html(spec: dict, accent_hsl: str = "240 6% 10%", freshness: dict | None = None) -> str:
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
        stamp_html = (
            f'<div class="stamp">Narrative composed {_fmt_stamp(freshness.get("composed_at", ""))}'
            f" · Data refreshed {_fmt_stamp(freshness.get('refreshed_at', ''))}</div>"
        )
    css = _CSS % {"accent": escape(accent_hsl)}
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>{css}</style></head><body><div class="report">'
        f'<div class="accent-bar"></div><h1>{title}</h1>{body}{stamp_html}{prov_html}</div></body></html>'
    )
