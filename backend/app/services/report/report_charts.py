from __future__ import annotations

import math
import re
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


def _bottom_pad(labels: list[str], rotate: bool) -> float:
    """Bottom padding to reserve for the x labels. Horizontal labels need the base
    ``_PAD_B``; rotated labels hang below their tick, so reserve their (ellipsis-capped)
    vertical extent too — otherwise a long angled label overflows the SVG viewport."""
    if not rotate:
        return float(_PAD_B)
    longest = max((len(_truncate_label(label)[0]) for label in labels), default=0)
    extent = min(longest, _MAX_LABEL_CHARS) * _CHAR_PX * math.sin(math.radians(_ROTATE_DEG))
    return _PAD_B + extent


def _tick_indices(n: int, k: int) -> list[int]:
    """Up to ``k`` evenly-spaced indices in ``[0, n)``, always including the first and
    last — i.e. WHICH x labels to actually stamp on a dense series. The unlabeled points
    still render; only their labels are thinned."""
    if n <= k:
        return list(range(n))
    stride = (n - 1) / (k - 1)
    return sorted({round(i * stride) for i in range(k)})


def _x_label(x: float, y: float, full: str, rotate: bool) -> str:
    """One x-axis label at ``(x, y)``: ellipsized display + a ``<title>`` tooltip carrying
    the full text when truncated; rotated ``-_ROTATE_DEG`` degrees (anchored at its tick
    end) when the axis is rotated, else horizontal + centered."""
    display, truncated = _truncate_label(full)
    tip = f"<title>{escape(full)}</title>" if truncated else ""
    if rotate:
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="12" font-weight="600" text-anchor="end" '
            f'fill="#111" transform="rotate(-{_ROTATE_DEG} {x:.1f} {y:.1f})">{escape(display)}{tip}</text>'
        )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="12" font-weight="600" text-anchor="middle" '
        f'fill="#111">{escape(display)}{tip}</text>'
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
        rows = rows[:_MAX_BAR_CATEGORIES]
    labels = [str(row.get(c.x_axis.key, "")) for row in rows]
    rotate = _should_rotate(labels)
    pad_b = _bottom_pad(labels, rotate)
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - pad_b
    bottom = _PAD_T + plot_h
    vmax, vmin, span = _value_range(rows, series)
    # y of value 0 (the baseline). All-zero data (vmax==vmin==0) → baseline at the BOTTOM
    # with zero-height bars, not collapsed to the top.
    base_y = bottom if vmax == vmin else _PAD_T + plot_h * (vmax / span)
    group_w = plot_w / len(rows)
    bar_w = group_w / (len(series) + 1)
    out = [
        f'<line x1="{_PAD_L}" y1="{base_y:.1f}" x2="{_W - _PAD_R}" y2="{base_y:.1f}" stroke="#000" stroke-width="2"/>'
    ]
    for i, row in enumerate(rows):
        gx = _PAD_L + i * group_w
        for j, s in enumerate(series):
            v = _num(row, s.key)
            h = abs(v) / span * plot_h
            x = gx + bar_w * (j + 0.5)
            # positives rise above the baseline; negatives drop below it (never a negative height)
            y = base_y - h if v >= 0 else base_y
            color = _safe_color(s.color, _PALETTE[j % len(_PALETTE)])
            # hard offset shadow (no blur) then the bar
            out.append(f'<rect x="{x + 4:.1f}" y="{y + 4:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="#000"/>')
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{color}" stroke="#000" stroke-width="2"/>'
            )
        out.append(_x_label(gx + group_w / 2, bottom + (16 if rotate else 20), labels[i], rotate))
    out.append(
        f'<text x="{_PAD_L - 8}" y="{_PAD_T + 8}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmax)}</text>'
    )
    if vmin < 0:
        out.append(
            f'<text x="{_PAD_L - 8}" y="{bottom}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmin)}</text>'
        )
    if capped:
        out.append(
            f'<text x="{_W - _PAD_R}" y="44" font-size="11" text-anchor="end" fill="#666">'
            f"Showing {_MAX_BAR_CATEGORIES} of {total_cats} categories</text>"
        )
    return "".join(out)


def _lines(c: ChartData, area: bool) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    labels = [str(r.get(c.x_axis.key, "")) for r in rows]
    # Keep EVERY data point on the line, but thin the LABELS on a dense series so a long
    # monthly trend doesn't stamp an overlapping label under all 24+ points.
    ticks = _tick_indices(len(rows), _MAX_AXIS_TICKS)
    rotate = _should_rotate([labels[i] for i in ticks])
    pad_b = _bottom_pad([labels[i] for i in ticks], rotate)
    plot_w = _W - _PAD_L - _PAD_R
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
        f'<line x1="{_PAD_L}" y1="{base_y:.1f}" x2="{_W - _PAD_R}" y2="{base_y:.1f}" stroke="#000" stroke-width="2"/>'
    ]
    for j, s in enumerate(series):
        color = _safe_color(s.color, _PALETTE[j % len(_PALETTE)])
        pts = [(_PAD_L + i * step, _y(_num(r, s.key))) for i, r in enumerate(rows)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area:
            poly = f"{_PAD_L},{base_y:.1f} " + path + f" {_PAD_L + (len(rows) - 1) * step:.1f},{base_y:.1f}"
            out.append(f'<polygon points="{poly}" fill="{color}" fill-opacity="0.25"/>')
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="3"/>')
        for x, y in pts:
            out.append(
                f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" '
                f'fill="{color}" stroke="#000" stroke-width="2"/>'
            )
    for i in ticks:
        out.append(_x_label(_PAD_L + i * step, bottom + (16 if rotate else 20), labels[i], rotate))
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
    cx, cy, rad = _W / 2, _H / 2 + 10, 130
    out, ang = [], -math.pi / 2
    for i, mag in enumerate(magnitudes):
        frac = mag / total
        # A single slice at (or ~) 100% is a degenerate SVG arc — its start and end points
        # coincide, so an <path> A-arc draws nothing. Render a full <circle> instead.
        if frac >= 0.999:
            out.append(
                f'<circle cx="{cx}" cy="{cy}" r="{rad}" fill="{_PALETTE[i % len(_PALETTE)]}" '
                f'stroke="#000" stroke-width="2"/>'
            )
            continue
        a2 = ang + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
        x2, y2 = cx + rad * math.cos(a2), cy + rad * math.sin(a2)
        out.append(
            f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{rad},{rad} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{_PALETTE[i % len(_PALETTE)]}" stroke="#000" stroke-width="2"/>'
        )
        ang = a2
    return "".join(out)


def render_chart_svg(chart: ChartData) -> str:
    t = chart.chart_type
    if t == "bar":
        return _frame(_bars(chart), chart.title)
    if t == "line":
        return _frame(_lines(chart, area=False), chart.title)
    if t == "area":
        return _frame(_lines(chart, area=True), chart.title)
    if t == "pie":
        return _frame(_pie(chart), chart.title)
    placeholder = (
        f'<text x="{_W / 2}" y="{_H / 2}" font-size="14" text-anchor="middle" fill="#666">'
        f'Chart type "{escape(t)}" not yet supported</text>'
    )
    return _frame(placeholder, chart.title)
