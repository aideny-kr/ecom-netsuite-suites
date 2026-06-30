from __future__ import annotations

import re
from html import escape

from app.schemas.chart import ChartData

_W, _H = 720, 380
_PAD_L, _PAD_B, _PAD_T, _PAD_R = 64, 56, 48, 24
_PALETTE = ["#6366f1", "#ef4444", "#f59e0b", "#10b981", "#0ea5e9", "#a855f7"]

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
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


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
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    bottom = _PAD_T + plot_h
    vmax, vmin, span = _value_range(rows, series)
    base_y = _PAD_T + plot_h * (vmax / span)  # y of value 0 (the baseline)
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
        label = escape(str(row.get(c.x_axis.key, "")))
        out.append(
            f'<text x="{gx + group_w / 2:.1f}" y="{bottom + 20}" font-size="12" font-weight="600" '
            f'text-anchor="middle" fill="#111">{label}</text>'
        )
    out.append(
        f'<text x="{_PAD_L - 8}" y="{_PAD_T + 8}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmax)}</text>'
    )
    if vmin < 0:
        out.append(
            f'<text x="{_PAD_L - 8}" y="{bottom}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmin)}</text>'
        )
    return "".join(out)


def _lines(c: ChartData, area: bool) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    bottom = _PAD_T + plot_h
    vmax, _vmin, span = _value_range(rows, series)
    base_y = _PAD_T + plot_h * (vmax / span)  # y of value 0

    def _y(v: float) -> float:
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
    for i, r in enumerate(rows):
        out.append(
            f'<text x="{_PAD_L + i * step:.1f}" y="{bottom + 20}" font-size="12" font-weight="600" '
            f'text-anchor="middle" fill="#111">{escape(str(r.get(c.x_axis.key, "")))}</text>'
        )
    return "".join(out)


def _pie(c: ChartData) -> str:
    import math

    rows = c.data
    key = c.y_axes[0].key if c.y_axes else None
    if not rows or not key:
        return ""
    total = sum(_num(r, key) for r in rows) or 1
    cx, cy, rad = _W / 2, _H / 2 + 10, 130
    out, ang = [], -math.pi / 2
    for i, r in enumerate(rows):
        frac = _num(r, key) / total
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
