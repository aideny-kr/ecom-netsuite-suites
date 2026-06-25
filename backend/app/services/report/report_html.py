from __future__ import annotations

import math
from html import escape

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
"""


def _fmt_amount(value) -> str:
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
    # A currency column may carry amounts serialized as STRINGS (SuiteQL returns e.g.
    # "1.6442836348665524E7" or "5,583,749.13") — coerce so they format; a non-numeric
    # string (e.g. "N/A") passes through unchanged.
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", ""))
        except ValueError:
            return value
    if not isinstance(value, (int, float)):
        return str(value)
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if not math.isfinite(n):  # NaN / Inf → blank, like a missing figure
        return ""
    # Round to cents FIRST, then pick the sign — so a tiny residual like -0.004 renders
    # a clean "0.00" rather than a misleading negative-signalling "(0.00)".
    n = round(n, 2)
    body = f"{abs(n):,.2f}"
    return f"({body})" if n < 0 else body


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
                    # _fmt_amount handles None/non-finite → "" and non-numeric → str().
                    cells.append(f'<td class="num">{escape(_fmt_amount(v))}</td>')
                elif v is None:
                    cells.append("<td></td>")  # null → empty cell, never "None"
                else:
                    cells.append(f"<td>{escape(str(v))}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        body = "".join(body_rows)
        note = ""
        if s.get("truncated"):
            note = f'<p class="foot">Showing first rows of {escape(str(s.get("row_count", "")))}.</p>'
        return (
            f'<div class="nb-card svg-wrap"><table><thead><tr>{cols}</tr></thead>'
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


def render_report_html(spec: dict, accent_hsl: str = "240 6% 10%") -> str:
    title = escape(str(spec.get("title", "Report")))
    body = "".join(_section_html(s) for s in spec.get("sections", []))
    prov = spec.get("provenance", {}) or {}
    sources = prov.get("sources", [])
    prov_html = ""
    if sources:
        items = "".join(f"<li>{escape(str(x))}</li>" for x in sources)
        prov_html = f'<div class="prov"><strong>Sources &amp; definitions</strong><ul>{items}</ul></div>'
    css = _CSS % {"accent": escape(accent_hsl)}
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>{css}</style></head><body><div class="report">'
        f'<div class="accent-bar"></div><h1>{title}</h1>{body}{prov_html}</div></body></html>'
    )
