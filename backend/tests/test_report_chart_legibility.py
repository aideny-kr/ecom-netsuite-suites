"""Phase 1 — chart legibility (report_charts renderer).

The live Framework report smeared its x-axis into an unreadable black band: long
account names ("11010 - Intercompany Receivables") stamped under every one of ~36
bars with no rotation, no truncation, no category cap. These tests pin the renderer
fixes structurally (rotation attrs, label counts, ellipsis + <title> tooltip,
dynamic bottom padding), not by brittle substring peeks — see the spec's note that
weak SVG-substring assertions have bitten this surface before.
"""

from __future__ import annotations

import math
import re

from app.schemas.chart import ChartAxis, ChartData
from app.services.report import report_charts as rc
from app.services.report.report_charts import render_chart_svg


def _bar_chart(categories: list[str], key: str = "account", value_key: str = "amount") -> ChartData:
    return ChartData(
        chart_type="bar",
        title="Drivers",
        x_axis=ChartAxis(label=key, key=key),
        y_axes=[ChartAxis(label=value_key, key=value_key, color="#6366f1")],
        data=[{key: c, value_key: (i + 1) * 100} for i, c in enumerate(categories)],
    )


def _line_chart(periods: list[str], key: str = "period", value_key: str = "cash") -> ChartData:
    return ChartData(
        chart_type="line",
        title="Cash Balance Trend",
        x_axis=ChartAxis(label=key, key=key),
        y_axes=[ChartAxis(label=value_key, key=value_key, color="#6366f1")],
        data=[{key: p, value_key: (i + 1) * 1000} for i, p in enumerate(periods)],
    )


def _rotated_labels(svg: str) -> list[str]:
    """Every <text> drawn on a rotation transform — i.e. a rotated axis label."""
    return re.findall(r'transform="rotate\(-?\d+', svg)


def _baseline_y(svg: str) -> float:
    """y of the first <line> — the zero baseline / x-axis rule the renderer draws first."""
    return float(re.search(r'<line x1="[\d.]+" y1="([\d.]+)"', svg).group(1))


# ---------------------------------------------------------------------------
# Rotation: many categories OR long labels must not smear.
# ---------------------------------------------------------------------------
def test_few_short_labels_are_not_rotated():
    """Regression guard: a small chart with short labels stays plain (middle-anchored),
    NOT rotated — rotation is a legibility escape hatch, not the default."""
    svg = render_chart_svg(_bar_chart(["Q1", "Q2"]))
    assert "rotate(" not in svg
    assert 'text-anchor="middle"' in svg


def test_many_categories_rotate_x_labels():
    svg = render_chart_svg(_bar_chart([f"Acct {i}" for i in range(8)]))  # 8 > _ROTATE_CATEGORIES_OVER
    assert len(_rotated_labels(svg)) == 8  # every visible category label rotated
    assert 'text-anchor="end"' in svg  # rotated labels anchor at their tick end


def test_long_label_triggers_rotation_even_when_few():
    """Only 3 categories, but one label is long → the axis rotates so it doesn't overlap."""
    svg = render_chart_svg(_bar_chart(["A", "Intercompany Receivables", "B"]))
    assert len(_rotated_labels(svg)) == 3


# ---------------------------------------------------------------------------
# Truncation: long labels ellipsized in-place, full text preserved in a tooltip.
# ---------------------------------------------------------------------------
def test_long_label_truncated_with_title_tooltip():
    full = "11010 - Intercompany Receivables"  # 32 chars > _MAX_LABEL_CHARS
    svg = render_chart_svg(_bar_chart(["A", full]))
    assert "…" in svg  # the visible label is ellipsized
    # the full text survives ONLY inside a <title> tooltip, never as a visible label
    assert f"<title>{full}</title>" in svg
    assert svg.count(full) == 1


# ---------------------------------------------------------------------------
# Category cap: a bar chart never renders a smear of dozens of bars.
# ---------------------------------------------------------------------------
def test_bar_category_cap_renders_subset_with_disclosed_note():
    cats = [f"Account {i:02d}" for i in range(20)]  # 20 > _MAX_BAR_CATEGORIES
    svg = render_chart_svg(_bar_chart(cats))
    assert svg.count('fill="#6366f1"') == rc._MAX_BAR_CATEGORIES  # only the cap's bars drawn
    assert len(_rotated_labels(svg)) == rc._MAX_BAR_CATEGORIES  # only the cap's labels drawn
    assert "of 20 categories" in svg  # and the truncation discloses the TRUE total


# ---------------------------------------------------------------------------
# Dynamic padding: rotating labels reserves more bottom room (no viewport overflow).
# ---------------------------------------------------------------------------
def test_rotation_reserves_more_bottom_room():
    short = render_chart_svg(_bar_chart(["Q1", "Q2"]))
    long = render_chart_svg(_bar_chart(["Long Account Name One", "Long Account Name Two"]))
    # the long/rotated chart pushes its plot bottom UP to make room for the angled labels
    assert _baseline_y(long) < _baseline_y(short)


# ---------------------------------------------------------------------------
# Line thinning: keep every data point, but never stamp a label under all of them.
# ---------------------------------------------------------------------------
def test_line_keeps_all_points_but_thins_labels():
    periods = [f"2026-{m:02d}" for m in range(1, 13)] + [f"2027-{m:02d}" for m in range(1, 13)]  # 24
    svg = render_chart_svg(_line_chart(periods))
    # all 24 data-point markers are drawn — thinning must not drop DATA
    assert svg.count('width="8" height="8"') == 24
    # but the x labels are thinned to a legible budget (kept endpoints, ≤ the tick cap)
    labels = _rotated_labels(svg) if "rotate(" in svg else re.findall(r'text-anchor="middle"', svg)
    assert 2 <= len(labels) <= rc._MAX_AXIS_TICKS


# ---------------------------------------------------------------------------
# Left-edge clip: a rotated label is END-anchored and swings DOWN-LEFT of its tick, so
# rotation must reserve HORIZONTAL room too — not just the vertical hang. The first
# tick on a LINE chart sits exactly at the left pad (no half-group offset like bars),
# so a long first label is the one that clips past x=0.
# ---------------------------------------------------------------------------
def _first_rotated_label_left_extent(svg: str) -> float:
    """Leftmost x reached by the first rotated x-label (end-anchored + rotate(-deg))."""
    m = re.search(
        r'<text x="([\d.]+)" y="([\d.]+)"[^>]*text-anchor="end"[^>]*transform="rotate\(-(\d+)[^>]*>([^<]*)',
        svg,
    )
    assert m, "expected a rotated end-anchored x label"
    x, deg, display = float(m.group(1)), float(m.group(3)), m.group(4)
    # end-anchored text occupies [x - w, x]; rotate(-deg) about x maps the far end to x - w*cos(deg)
    return x - len(display) * rc._CHAR_PX * math.cos(math.radians(deg))


def test_rotated_line_first_label_not_clipped_past_left_edge():
    # A monthly line whose first label is long (triggers rotation). Line charts anchor
    # the first tick at the left pad, so without horizontal compensation the end-anchored
    # rotated label swings past x=0 and is clipped by the SVG viewport (viewBox starts at 0).
    periods = ["September 2026"] + [f"M{i}" for i in range(2, 14)]  # 13 pts, long first label
    left = _first_rotated_label_left_extent(render_chart_svg(_line_chart(periods)))
    assert left >= 0, f"first line label clips past x=0 (leftmost={left:.1f})"


def test_rotated_bar_first_label_not_clipped_past_left_edge():
    # Bars are borderline-safe (half-group offset), but the leftmost angled label must
    # still stay within the viewport for long labels — a regression guard.
    cats = ["Intercompany Receivables"] + [f"Account {i}" for i in range(2, 10)]
    left = _first_rotated_label_left_extent(render_chart_svg(_bar_chart(cats)))
    assert left >= 0, f"first bar label clips past x=0 (leftmost={left:.1f})"
