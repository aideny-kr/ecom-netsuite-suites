from __future__ import annotations

import math
import re
import unicodedata
from html import escape

from app.schemas.chart import ChartData

_W, _H = 720, 380
_PAD_L, _PAD_B, _PAD_T, _PAD_R = 64, 56, 48, 24
_PALETTE = ["#6366f1", "#ef4444", "#f59e0b", "#10b981", "#0ea5e9", "#a855f7"]

# --- x-axis label legibility -------------------------------------------------------
# The live report smeared its x-axis: long account names ("11010 - Intercompany
# Receivables") stamped under every one of ~36 un-rotated bars collided into an
# unreadable band. These bounds make the axis legible deterministically.
#
# Visible label cap: a longer label is ellipsized to _MAX_LABEL_CHARS chars ("…" is the
# last one) with the FULL text preserved in a <title> tooltip — nothing is hidden.
_MAX_LABEL_CHARS = 16
# Rotate the x labels (so they stop overlapping) once EITHER there are more than this
# many categories OR any label is longer than _ROTATE_LABEL_LEN_OVER characters.
_ROTATE_CATEGORIES_OVER = 6
_ROTATE_LABEL_LEN_OVER = 10
_ROTATE_DEG = 35  # angle the x labels are drawn at when rotation kicks in
# Bar charts refuse to render a smear: at most this many categories are drawn (in the
# caller's curated order — aggregating to a handful of comparable drivers is upstream's
# job, Phase 4), and the true total is disclosed in a note.
_MAX_BAR_CATEGORIES = 12
# Line/area charts keep EVERY data point (never drop a figure) but stamp at most this
# many x labels — evenly spaced, endpoints kept — so a long monthly series stays legible.
_MAX_AXIS_TICKS = 12
_CHAR_PX = 7  # rough advance width of the 12px label font, for rotated-label padding math

# ChartAxis.color is a free-form string that can originate from upstream tool output
# the LLM/data influences. The SVG is injected into the published report HTML RAW
# (report_html.py treats it as trusted), so an unvalidated color interpolated into
# fill="{color}" would let a crafted value break out of the attribute into executable
# SVG/HTML. Only accept well-formed hex (#rgb/#rrggbb/#rrggbbaa) or hsl()/hsla();
# anything else falls back to the palette default at the call site.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^hsla?\([0-9R, .%]*\)$")


def _safe_color(color: str | None, default: str) -> str:
    """Return ``color`` only if it is a well-formed hex/hsl color; else ``default``."""
    if isinstance(color, str) and _COLOR_RE.match(color):
        return color
    return default


def _fmt(v: float) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def _tip_num(v: float) -> str:
    """Full-precision thousands-separated figure for a hover tooltip (Slice D) — the
    tooltip is where exact numbers belong, never the '1.2M' axis abbreviation. An
    integral float drops its '.0' noise."""
    s = f"{v:,}"
    return s[:-2] if s.endswith(".0") else s


def _tip(category: str, series_label: str, v: float) -> str:
    """A native SVG ``<title>`` — the browser renders it as a hover tooltip with zero
    JS/CSS, so it works even inside the FE viewer's sandbox="" iframe."""
    return f"<title>{escape(category)} — {escape(str(series_label))}: {_tip_num(v)}</title>"


def _legend(entries: list[tuple[str, str]], *, toggles: bool) -> str:
    """The legend appended AFTER ``</svg>`` in the same returned string (the artifact
    stays one self-contained document). ``entries`` = [(label, safe_color)] in series
    order. ``toggles=True`` emits label-WRAPPED checkboxes (no id/for pairs — several
    charts per report would collide ids) whose ``ser-j`` class the CSS-only toggle
    rules in report_html._CSS bind to via :has(); unsupported browsers degrade to
    inert checkboxes. Colors must already be _safe_color-vetted by the caller."""
    items = []
    for j, (label, color) in enumerate(entries):
        swatch = f'<span class="swatch" style="background:{color}"></span>'
        if toggles:
            items.append(f'<label><input type="checkbox" class="ser-{j}" checked>{swatch}{escape(str(label))}</label>')
        else:
            items.append(f"<label>{swatch}{escape(str(label))}</label>")
    return f'<div class="chart-legend">{"".join(items)}</div>'


def _truncate_label(text: str) -> tuple[str, bool]:
    """``(display, truncated)``. Ellipsize a label longer than ``_MAX_LABEL_CHARS`` so it
    fits its slot; the caller preserves the full text in a ``<title>`` tooltip when
    ``truncated`` is True, so no figure is ever hidden."""
    if len(text) <= _MAX_LABEL_CHARS:
        return text, False
    return text[: _MAX_LABEL_CHARS - 1].rstrip() + "…", True


def _should_rotate(labels: list[str]) -> bool:
    """Rotate the x axis when the labels would otherwise collide: many categories, or any
    single label long enough to overrun its slot."""
    return len(labels) > _ROTATE_CATEGORIES_OVER or any(len(label) > _ROTATE_LABEL_LEN_OVER for label in labels)


def _text_reach_px(text: str) -> float:
    """Approximate rendered px width of ``text``. Wide/fullwidth glyphs (CJK, kana) are
    ~2x a Latin char at this font size, so count them double — measuring by char COUNT
    would underestimate a CJK label's width and let it clip the viewport (the product
    targets NetSuite OneWorld, so international account names are expected)."""
    return _CHAR_PX * sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _label_pads(labels: list[str], rotate: bool) -> tuple[float, float]:
    """``(left, bottom)`` padding to reserve for the x labels. Horizontal labels need only
    the base ``_PAD_L`` / ``_PAD_B``. A rotated label is END-anchored and swings DOWN-LEFT
    of its tick, so reserve BOTH its horizontal extent (else the leftmost long label clips
    past ``x=0`` — line charts anchor the first tick exactly at the left pad) AND its
    vertical extent (else a low label overflows the bottom of the SVG viewport)."""
    if not rotate:
        return float(_PAD_L), float(_PAD_B)
    # Measure the PIXEL reach of each (already ellipsis-capped) display, not its char
    # count — wide/CJK glyphs are ~2x, and undercounting them re-clips the viewport.
    reach = max((_text_reach_px(_truncate_label(label)[0]) for label in labels), default=0.0)
    rad = math.radians(_ROTATE_DEG)
    left = max(float(_PAD_L), reach * math.cos(rad) + 6)  # +6px margin off the left edge
    return left, _PAD_B + reach * math.sin(rad)


def _tick_indices(n: int, k: int) -> list[int]:
    """Up to ``k`` evenly-spaced indices in ``[0, n)``, always including the first and
    last — i.e. WHICH x labels to actually stamp on a dense series. The unlabeled points
    still render; only their labels are thinned."""
    if n <= k:
        return list(range(n))
    stride = (n - 1) / (k - 1)
    return sorted({round(i * stride) for i in range(k)})


def _x_values(x_key: str, rows: list[dict]) -> list[str]:
    """The x-axis label string for each plotted row (missing key → '')."""
    return [str(row.get(x_key, "")) for row in rows]


def _x_label(x: float, bottom: float, full: str, rotate: bool) -> str:
    """One x-axis label whose tick is at ``x`` on the axis line ``bottom``: ellipsized
    display + a ``<title>`` tooltip carrying the full text when truncated; rotated
    ``-_ROTATE_DEG`` degrees (end-anchored) when the axis is rotated, else horizontal +
    centered. Rotated labels sit closer to the axis (they hang below it)."""
    display, truncated = _truncate_label(full)
    tip = f"<title>{escape(full)}</title>" if truncated else ""
    y = bottom + (16 if rotate else 20)
    anchor = "end" if rotate else "middle"
    transform = f' transform="rotate(-{_ROTATE_DEG} {x:.1f} {y:.1f})"' if rotate else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="12" font-weight="600" '
        f'text-anchor="{anchor}" fill="#111"{transform}>{escape(display)}{tip}</text>'
    )


def _frame(body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" viewBox="0 0 {_W} {_H}" '
        f"font-family=\"'Inter',system-ui,sans-serif\">"
        f'<rect x="2" y="2" width="{_W - 4}" height="{_H - 4}" fill="#FFFFFF" stroke="#000" stroke-width="3"/>'
        f'<text x="20" y="30" font-size="18" font-weight="800" fill="#111">{escape(title)}</text>'
        f"{body}</svg>"
    )


def _num(row: dict, key: str) -> float:
    try:
        n = float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    # A non-finite value (NaN/Inf — possible via a prebuilt chart_data payload that skips
    # the tabular coerce guard) must not bake a NaN/Inf coordinate into the SVG.
    return n if math.isfinite(n) else 0.0


def _value_range(rows: list, series: list) -> tuple[float, float, float]:
    """(vmax, vmin, span) over all plotted values, ALWAYS including 0 so the zero
    baseline is in range: an all-positive series floors at 0, an all-negative series
    tops at 0, and mixed-sign data places the baseline between. span is never 0."""
    vals = [_num(r, s.key) for r in rows for s in series]
    vmax = max(vals + [0.0])
    vmin = min(vals + [0.0])
    return vmax, vmin, (vmax - vmin) or 1.0


def _bars(c: ChartData) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    # Legibility cap: never render a smear of dozens of bars. Keep the caller's order
    # (curating to a handful of comparable drivers is upstream's job — Phase 4) and
    # disclose the true total in a note below.
    total_cats = len(rows)
    capped = total_cats > _MAX_BAR_CATEGORIES
    if capped:
        # Keep the _MAX_BAR_CATEGORIES MOST MATERIAL rows (largest |value| across series),
        # NOT the first N in source order: slicing source-order rows before _value_range
        # would drop a large-magnitude driver past the cut AND rescale the y-axis to the
        # smaller visible subset — a materially misleading financial chart (T2 gate: major).
        # Kept rows render in their original relative order. (_MAX_BAR_CATEGORIES ==
        # report_service._REPORT_TABLE_TOP_K, so a pre-curated auto-chart never caps here;
        # this guards the explicit-chart path, which delivers up to 100 unranked rows.)
        def _mag(row: dict) -> float:
            return max((abs(_num(row, s.key)) for s in series), default=0.0)

        keep = sorted(sorted(range(len(rows)), key=lambda i: _mag(rows[i]), reverse=True)[:_MAX_BAR_CATEGORIES])
        rows = [rows[i] for i in keep]
    labels = _x_values(c.x_axis.key, rows)
    rotate = _should_rotate(labels)
    pad_l, pad_b = _label_pads(labels, rotate)
    plot_w = _W - pad_l - _PAD_R
    plot_h = _H - _PAD_T - pad_b
    bottom = _PAD_T + plot_h
    vmax, vmin, span = _value_range(rows, series)
    # y of value 0 (the baseline). All-zero data (vmax==vmin==0) → baseline at the BOTTOM
    # with zero-height bars, not collapsed to the top.
    base_y = bottom if vmax == vmin else _PAD_T + plot_h * (vmax / span)
    group_w = plot_w / len(rows)
    bar_w = group_w / (len(series) + 1)
    out = [
        f'<line x1="{pad_l:.1f}" y1="{base_y:.1f}" x2="{_W - _PAD_R}" y2="{base_y:.1f}" '
        'stroke="#000" stroke-width="2"/>'
    ]
    for i, row in enumerate(rows):
        gx = pad_l + i * group_w
        for j, s in enumerate(series):
            v = _num(row, s.key)
            h = abs(v) / span * plot_h
            x = gx + bar_w * (j + 0.5)
            # positives rise above the baseline; negatives drop below it (never a negative height)
            y = base_y - h if v >= 0 else base_y
            color = _safe_color(s.color, _PALETTE[j % len(_PALETTE)])
            # One <g> per datum serves BOTH Slice-D features: class ser-j is the CSS
            # series-toggle hook, <title> is the native hover value. The <rect> strings
            # stay byte-identical (shadow then bar) — geometry tests anchor on them.
            out.append(f'<g class="ser-{j}">{_tip(labels[i], s.label, v)}')
            out.append(f'<rect x="{x + 4:.1f}" y="{y + 4:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="#000"/>')
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{color}" stroke="#000" stroke-width="2"/>'
            )
            out.append("</g>")
        out.append(_x_label(gx + group_w / 2, bottom, labels[i], rotate))
    out.append(
        f'<text x="{pad_l - 8:.1f}" y="{_PAD_T + 8}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmax)}</text>'
    )
    if vmin < 0:
        out.append(
            f'<text x="{pad_l - 8:.1f}" y="{bottom:.1f}" font-size="11" text-anchor="end" fill="#444">'
            f"{_fmt(vmin)}</text>"
        )
    if capped:
        out.append(
            f'<text x="{_W - _PAD_R}" y="44" font-size="11" text-anchor="end" fill="#666">'
            f"Showing {_MAX_BAR_CATEGORIES} largest of {total_cats} categories</text>"
        )
    return "".join(out)


def _lines(c: ChartData, area: bool) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    labels = _x_values(c.x_axis.key, rows)
    # Keep EVERY data point on the line, but thin the LABELS on a dense series so a long
    # monthly trend doesn't stamp an overlapping label under all 24+ points.
    ticks = _tick_indices(len(rows), _MAX_AXIS_TICKS)
    shown = [labels[i] for i in ticks]
    rotate = _should_rotate(shown)
    pad_l, pad_b = _label_pads(shown, rotate)
    plot_w = _W - pad_l - _PAD_R
    plot_h = _H - _PAD_T - pad_b
    bottom = _PAD_T + plot_h
    vmax, vmin, span = _value_range(rows, series)
    base_y = bottom if vmax == vmin else _PAD_T + plot_h * (vmax / span)  # y of value 0

    def _y(v: float) -> float:
        if vmax == vmin:
            return bottom
        return _PAD_T + plot_h * (vmax - v) / span

    step = plot_w / max(len(rows) - 1, 1)
    out = [
        f'<line x1="{pad_l:.1f}" y1="{base_y:.1f}" x2="{_W - _PAD_R}" y2="{base_y:.1f}" '
        'stroke="#000" stroke-width="2"/>'
    ]
    for j, s in enumerate(series):
        color = _safe_color(s.color, _PALETTE[j % len(_PALETTE)])
        pts = [(pad_l + i * step, _y(_num(r, s.key))) for i, r in enumerate(rows)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        # the whole series (fill + line + point markers) shares one ser-j toggle group
        out.append(f'<g class="ser-{j}">')
        if area:
            poly = f"{pad_l:.1f},{base_y:.1f} " + path + f" {pad_l + (len(rows) - 1) * step:.1f},{base_y:.1f}"
            out.append(f'<polygon points="{poly}" fill="{color}" fill-opacity="0.25"/>')
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="3"/>')
        for i, (x, y) in enumerate(pts):
            out.append(
                f"<g>{_tip(labels[i], s.label, _num(rows[i], s.key))}"
                f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" '
                f'fill="{color}" stroke="#000" stroke-width="2"/></g>'
            )
        out.append("</g>")
    for i in ticks:
        out.append(_x_label(pad_l + i * step, bottom, labels[i], rotate))
    return "".join(out)


def _pie(c: ChartData) -> str:
    rows = c.data
    key = c.y_axes[0].key if c.y_axes else None
    if not rows or not key:
        return ""
    # A pie shows magnitude composition: use |value| so a negative datum is a real slice
    # (a signed fraction would draw an inverted/overlapping arc). Financial data has
    # negatives, and an explicit `pie` over it must still render sane slices.
    magnitudes = [abs(_num(r, key)) for r in rows]
    total = sum(magnitudes) or 1
    labels = _x_values(c.x_axis.key, rows)
    series_label = c.y_axes[0].label
    cx, cy, rad = _W / 2, _H / 2 + 10, 130
    out, ang = [], -math.pi / 2
    for i, mag in enumerate(magnitudes):
        # the tooltip carries the REAL signed value — |v| is geometry only
        tip = _tip(labels[i], series_label, _num(rows[i], key))
        frac = mag / total
        # A single slice at (or ~) 100% is a degenerate SVG arc — its start and end points
        # coincide, so an <path> A-arc draws nothing. Render a full <circle> instead.
        if frac >= 0.999:
            out.append(
                f'<g>{tip}<circle cx="{cx}" cy="{cy}" r="{rad}" fill="{_PALETTE[i % len(_PALETTE)]}" '
                f'stroke="#000" stroke-width="2"/></g>'
            )
            continue
        a2 = ang + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
        x2, y2 = cx + rad * math.cos(a2), cy + rad * math.sin(a2)
        out.append(
            f'<g>{tip}<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{rad},{rad} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{_PALETTE[i % len(_PALETTE)]}" stroke="#000" stroke-width="2"/></g>'
        )
        ang = a2
    return "".join(out)


def _series_legend(chart: ChartData) -> str:
    """Checkbox-toggle legend for a MULTI-series chart (≥2 y axes). A single series
    gets nothing: a one-entry legend is noise and toggling the only series off would
    blank the chart."""
    if not chart.data or len(chart.y_axes) < 2:
        return ""
    entries = [
        (s.label, _safe_color(s.color, _PALETTE[j % len(_PALETTE)])) for j, s in enumerate(chart.y_axes)
    ]
    return _legend(entries, toggles=True)


def _pie_legend(chart: ChartData) -> str:
    """Static color→category key (pies previously shipped with no key at all). NO
    checkboxes: slices are categories, not series — CSS-hiding one leaves a wedge
    hole that misrepresents the remaining shares (angles can't recompute without JS),
    which would be dishonest interactivity."""
    if not chart.data or not chart.y_axes:
        return ""
    labels = _x_values(chart.x_axis.key, chart.data)
    entries = [(labels[i], _PALETTE[i % len(_PALETTE)]) for i in range(len(chart.data))]
    return _legend(entries, toggles=False)


def render_chart_svg(chart: ChartData) -> str:
    t = chart.chart_type
    if t == "bar":
        return _frame(_bars(chart), chart.title) + _series_legend(chart)
    if t == "line":
        return _frame(_lines(chart, area=False), chart.title) + _series_legend(chart)
    if t == "area":
        return _frame(_lines(chart, area=True), chart.title) + _series_legend(chart)
    if t == "pie":
        return _frame(_pie(chart), chart.title) + _pie_legend(chart)
    placeholder = (
        f'<text x="{_W / 2}" y="{_H / 2}" font-size="14" text-anchor="middle" fill="#666">'
        f'Chart type "{escape(t)}" not yet supported</text>'
    )
    return _frame(placeholder, chart.title)
