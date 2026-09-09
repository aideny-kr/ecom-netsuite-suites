"""Task 2 (Slice 1) — render the ``inventory_aging`` playbook's `AgingReport` (Task 1)
as the approved mock.

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
Part A. Mock: the binding layout/labels/copy reference (real tenant numbers — read for
structure only, never pasted into this file per report-design.md #1 and the plan's
Global Constraints). This module's fixture is entirely SYNTHETIC (fake locations/SKUs/
dollar values), independent of Task 1's own ``test_inventory_aging.py`` fixtures
(those deliberately exercise top-5 truncation with 6 aged items in one location — this
module needs the OPPOSITE: every location's aged-item count at or below
``TOP_ITEMS_PER_LOCATION`` (5), so Task 1's ``top_items`` tuple genuinely IS the full
aged list and "nothing truncated" is a fact this test can assert, not a claim it can't
back up — see the concern noted in the implementer's report about Task 1's AgingReport
not separately carrying a full (unbounded) aged-item list).

Seam: ``build_inventory_aging_sections(report)`` turns a computed ``AgingReport`` into
the section list ``render_report_html`` already knows how to join — the SAME
pre-computed "model" seam ``financial_statement`` sections use (see
``_financial_statement_html``'s docstring), not a new contract.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.report.inventory_aging import compute
from app.services.report.report_html import (
    build_inventory_aging_provenance,
    build_inventory_aging_sections,
    render_report_html,
)

SNAPSHOT = date(2026, 9, 8)
LOCATIONS = ("Nova", "Solace")

_REQUIRED_HEADINGS = (
    "Watch items",
    "Aged share of on-hand value, by location",
    "By location",
    "Aging buckets by location",
    "Largest aged positions",
    "Highlights",
    "Narrative",
)


def _item(location, sku, days, value, qty, *, desc="Widget", category="Misc"):
    return {
        "location": location,
        "sku": sku,
        "item_desc": desc,
        "category": category,
        "qty_on_hand": qty,
        "inventory_amount": value,
        "snapshot_date": SNAPSHOT.isoformat(),
        "last_restock_date": (SNAPSHOT - timedelta(days=days)).isoformat(),
        "days": days,
        "bucket": "WRONG-ON-PURPOSE",  # compute() derives it from days, never trusts this
    }


def _prior_row(location, *, value, value_90p, value_180p, skus, skus_90p, skus_180p, qty, qty_90p):
    return {
        "location": location,
        "skus": skus,
        "qty": qty,
        "value": value,
        "skus_90p": skus_90p,
        "qty_90p": qty_90p,
        "value_90p": value_90p,
        "value_180p": value_180p,
        "skus_180p": skus_180p,
    }


def _trend_row(location, d, total_value, value_90p, pct_90p):
    return {
        "location": location,
        "d": d.isoformat(),
        "total_value": total_value,
        "value_90p": value_90p,
        "pct_90p": pct_90p,
    }


def _fixture():
    """Two synthetic locations. Nova: 3 current + 3 aged (91-180 x1, 180+ x2). Solace:
    2 current + 1 aged (91-180 x1). Every aged bucket therefore has <= 5 items per
    location -- ``top_items`` (capped at TOP_ITEMS_PER_LOCATION=5) is the COMPLETE aged
    list here, not a truncated slice, which is what makes the "full aged list ...
    complete" assertion below honest. Solace's prior value (70000) exceeds its current
    on-hand value (61000) on purpose, so ``delta_value``/``delta_pct`` are NEGATIVE for
    at least one location -- the fixture needed to exercise "negative deltas render in
    parentheses"."""
    items = [
        _item("Nova", "NOV-A1", 10, 50000, 100, desc="Alpha Widget", category="Widgets"),
        _item("Nova", "NOV-A2", 45, 20000, 50, desc="Beta Widget", category="Widgets"),
        _item("Nova", "NOV-A3", 75, 15000, 30, desc="Gamma Widget", category="Widgets"),
        _item("Nova", "NOV-A4", 120, 80000, 20, desc="Delta Widget", category="Widgets"),
        _item("Nova", "NOV-A5", 200, 60000, 10, desc="Epsilon Widget", category="Widgets"),
        _item("Nova", "NOV-A6", 210, 30000, 5, desc="Zeta Widget", category="Widgets"),
        _item("Solace", "SOL-B1", 5, 12000, 40, desc="Nu Gadget", category="Gadgets"),
        _item("Solace", "SOL-B2", 55, 9000, 30, desc="Xi Gadget", category="Gadgets"),
        _item("Solace", "SOL-B3", 140, 40000, 15, desc="Omicron Gadget", category="Gadgets"),
    ]
    prior = [
        _prior_row(
            "Nova",
            value=230000,
            value_90p=70000,
            value_180p=20000,
            skus=6,
            skus_90p=3,
            skus_180p=1,
            qty=210,
            qty_90p=40,
        ),
        _prior_row(
            "Solace", value=70000, value_90p=18000, value_180p=0, skus=3, skus_90p=1, skus_180p=0, qty=90, qty_90p=15
        ),
    ]
    weekly = {
        "Nova": [(2, 240000, 80000), (1, 245000, 85000), (0, 255000, 90000)],
        "Solace": [(2, 58000, 22000), (1, 60000, 24000), (0, 61000, 40000)],
    }
    trend = []
    for loc, points in weekly.items():
        for weeks_ago, total_value, value_90p in points:
            d = SNAPSHOT - timedelta(weeks=weeks_ago)
            pct_90p = round(value_90p / total_value * 100, 1)
            trend.append(_trend_row(loc, d, total_value, value_90p, pct_90p))
    meta = [
        {
            "location": loc,
            "first_snapshot_date": (SNAPSHOT - timedelta(days=150)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 150,
        }
        for loc in LOCATIONS
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": list(LOCATIONS), "compare_days": 7, "trend_weeks": 3}
    return payloads, params


@pytest.fixture
def report():
    payloads, params = _fixture()
    return compute(payloads, params)


@pytest.fixture
def spec(report):
    return {
        "title": f"Inventory Aging — Week of {report.snapshot_date.isoformat()}",
        "sections": build_inventory_aging_sections(report),
    }


@pytest.fixture
def html(spec):
    return render_report_html(spec)


# ---------------------------------------------------------------------------
# No <script> tags anywhere (design rule #14 -- CSS-only / native <details> only)
# ---------------------------------------------------------------------------
def test_no_script_tags(html):
    assert "<script" not in html.lower()


# ---------------------------------------------------------------------------
# The mock's section headings, verbatim
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("heading", _REQUIRED_HEADINGS)
def test_section_heading_present_verbatim(html, heading):
    assert heading in html


def test_sources_and_method_heading_present(spec, report):
    prov = build_inventory_aging_provenance(report.provenance)
    out = render_report_html(spec, provenance=prov)
    assert "Sources &amp; method" in out


# ---------------------------------------------------------------------------
# KPI cards: arrows + favourable/unfavourable classes (spec §A1 "each: value, delta
# vs prior with up/down arrow and favourable colour")
# ---------------------------------------------------------------------------
def test_kpi_cards_render_for_every_kpi(html, report):
    assert html.count('class="kpi"') == len(report.kpis)


def test_kpi_cards_carry_arrows(html):
    assert "▲" in html or "▼" in html


def test_kpi_cards_carry_favourable_class(html, report):
    assert any(k.favourable for k in report.kpis)
    assert 'class="fav"' in html


def test_kpi_sparkline_svg_present(html):
    # every KPI in the fixture has a >=2-point sparkline (aged180's is deliberately
    # empty per Task 1 -- see KpiCard.sparkline's docstring -- so this only asserts
    # AT LEAST one renders, not all four).
    assert "<svg" in html
    assert "<polyline" in html


# ---------------------------------------------------------------------------
# Trend chart: inline SVG, one polyline per location, endpoint labels
# ---------------------------------------------------------------------------
def test_trend_chart_one_polyline_per_location(html, report):
    # KPI sparklines ALSO emit <polyline> -- isolate the trend chart's own <svg role="img">
    # block (the sparklines carry no role attribute) before counting.
    marker = 'role="img"'
    start = html.index(marker)
    chart_svg = html[start : html.index("</svg>", start) + len("</svg>")]
    assert chart_svg.count("<polyline") == len(report.locations)


def test_trend_chart_has_endpoint_value_labels(html, report):
    for loc in report.locations:
        last = report.trend[loc.location][-1]
        assert f"{last.pct_90p}%" in html


# ---------------------------------------------------------------------------
# Bucket table: Current/Aged subtotal rows + On hand total row (never truncated)
# ---------------------------------------------------------------------------
def test_bucket_table_has_current_and_aged_subtotals_and_on_hand_total(html):
    assert "Current (" in html
    assert "Aged (" in html
    assert ">On hand<" in html
    assert 'class="total"' in html
    assert 'class="sub"' in html


# ---------------------------------------------------------------------------
# Largest aged positions: full list present (collapsed) and complete
# ---------------------------------------------------------------------------
def test_full_aged_list_present_collapsed_and_complete(html, report):
    total_aged = sum(len(items) for items in report.top_items.values())
    assert total_aged == 4  # sanity on the fixture contract (3 Nova + 1 Solace)
    assert f"All {total_aged} aged SKUs" in html
    # collapsed by default -- a bare <details> (no `open` attribute) is closed on load
    assert "<details>" in html
    assert "<details open" not in html
    for items in report.top_items.values():
        for it in items:
            assert it.sku in html


# ---------------------------------------------------------------------------
# Negative deltas render in parentheses (design rule #9)
# ---------------------------------------------------------------------------
def test_negative_delta_value_renders_in_parentheses(html, report):
    solace = next(loc for loc in report.locations if loc.location == "Solace")
    assert solace.delta_value < 0  # sanity on the fixture contract
    assert f"({abs(solace.delta_value):,.0f})" in html


def test_negative_delta_pct_renders_in_parentheses(html, report):
    solace = next(loc for loc in report.locations if loc.location == "Solace")
    assert solace.delta_pct < 0  # sanity on the fixture contract
    assert f"({abs(solace.delta_pct)}%)" in html


# ---------------------------------------------------------------------------
# Highlights / narrative
# ---------------------------------------------------------------------------
def test_highlights_rendered_as_list_items(html, report):
    # escape() turns "Nova's" into "Nova&#x27;s" (quote=True, the XSS-safe default
    # shared by every other renderer in this module) -- compare against the
    # UNESCAPED html, same as the narrative check below.
    from html import unescape

    unescaped = unescape(html)
    assert html.count("<li>") >= len(report.highlights)
    for h in report.highlights:
        assert h.text in unescaped


def test_narrative_paragraphs_rendered(html, report):
    from html import unescape

    unescaped = unescape(html)
    assert report.narrative.paragraph_1 in unescaped
    assert report.narrative.paragraph_2 in unescaped


# ---------------------------------------------------------------------------
# CSS: no <script>, %-integrity canary (existing test pattern -- see
# test_fs_percent_integrity_canary_full_render_does_not_raise), print un-clips the
# collapsible aged list.
# ---------------------------------------------------------------------------
def test_ia_css_percent_integrity_canary_full_render_does_not_raise(html):
    assert "<style>" in html
    assert "ia-" in html


def test_print_media_unclips_the_collapsible_aged_list():
    from app.services.report.report_html import _IA_CSS

    assert "@media print" in _IA_CSS
    print_block = _IA_CSS.split("@media print", 1)[1]
    assert "details" in print_block


def test_render_report_html_deterministic(spec):
    assert render_report_html(spec) == render_report_html(spec)
