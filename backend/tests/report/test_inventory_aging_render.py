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
# Review finding (major): bucket-row labels must use the mock's en dash
# (U+2013), not an ASCII hyphen -- "0–30 days", not "0-30 days".
# ---------------------------------------------------------------------------
def test_bucket_row_labels_use_en_dash_not_ascii_hyphen(html):
    for en_dash_label in ("0–30 days", "31–60 days", "61–90 days", "91–180 days"):
        assert en_dash_label in html
    for ascii_label in ("0-30 days", "31-60 days", "61-90 days", "91-180 days"):
        assert ascii_label not in html
    # "180+ days" has no dash either way -- still present, unaffected by the fix.
    assert "180+ days" in html


# ---------------------------------------------------------------------------
# Largest aged positions: full list present (collapsed) and complete
# ---------------------------------------------------------------------------
def test_full_aged_list_present_collapsed_and_complete(html, report):
    # Sourced from aged_items (the UNBOUNDED field) -- not top_items -- because
    # top_items is capped at TOP_ITEMS_PER_LOCATION=5 and cannot back a "nothing
    # truncated" claim on its own (review finding). This fixture's own docstring
    # guarantees aged_items == top_items here (every location has <= 5 aged
    # items), which is what makes this assertion honest for THIS fixture; the
    # truncated-location fixture below is what actually exercises the two
    # fields diverging.
    total_aged = sum(len(items) for items in report.aged_items.values())
    assert total_aged == 4  # sanity on the fixture contract (3 Nova + 1 Solace)
    assert f"All {total_aged} aged SKUs" in html
    # collapsed by default -- a bare <details> (no `open` attribute) is closed on load
    assert "<details>" in html
    assert "<details open" not in html
    for items in report.aged_items.values():
        for it in items:
            assert it.sku in html


# ---------------------------------------------------------------------------
# Review finding (major): the per-location group-header prose ("<location> ·
# aged <value> · ...") must carry a literal "$" before the aged value -- it is
# prose, not a table cell, and the mock's binding copy reads e.g.
# "Dimerco · aged $3,230,556 · top 5 = 55.5%". Covers both the top-5 table's
# group header AND the "All N aged SKUs" details block's group header.
# ---------------------------------------------------------------------------
def test_group_header_prose_carries_dollar_sign_before_aged_value(html, report):
    for loc in report.locations:
        money = f"{loc.aged90_value:,.0f}"
        assert f"aged ${money}" in html
        # Every group-header occurrence carries the "$" -- none render bare.
        assert html.count(f"aged {money} ·") == 0


# ---------------------------------------------------------------------------
# Review finding (blocker): "All N aged SKUs" was built from top_items, which
# Task 1 caps at TOP_ITEMS_PER_LOCATION=5 -- a location with MORE than 5 aged
# SKUs got a false-completeness claim (N understated, the details block byte-
# identical to the top-5 table). This fixture deliberately has 7 aged SKUs in
# one location to exercise that divergence directly.
# ---------------------------------------------------------------------------
def _fixture_with_more_than_five_aged_in_one_location():
    items = [_item("Solo", f"SOLO-{i}", 100, 10000 - i * 100, 10) for i in range(7)]
    prior = [
        _prior_row(
            "Solo", value=100000, value_90p=50000, value_180p=0, skus=7, skus_90p=7, skus_180p=0, qty=70, qty_90p=70
        )
    ]
    trend = [_trend_row("Solo", SNAPSHOT - timedelta(weeks=w), 100000, 50000, 50.0) for w in (2, 1, 0)]
    meta = [
        {
            "location": "Solo",
            "first_snapshot_date": (SNAPSHOT - timedelta(days=150)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 150,
        }
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": ["Solo"], "compare_days": 7, "trend_weeks": 3}
    return payloads, params


def test_full_aged_list_details_block_carries_every_aged_sku_when_top5_truncates():
    payloads, params = _fixture_with_more_than_five_aged_in_one_location()
    truncated_report = compute(payloads, params)
    assert len(truncated_report.top_items["Solo"]) == 5  # sanity: top5 truncates
    assert len(truncated_report.aged_items["Solo"]) == 7  # sanity: aged_items does not
    spec = {"title": "t", "sections": build_inventory_aging_sections(truncated_report)}
    out = render_report_html(spec)

    # The claim must name the TRUE count (7), not the capped one (5).
    assert "All 7 aged SKUs" in out
    assert "All 5 aged SKUs" not in out

    # The details block's row count must actually differ from the top-5 table's --
    # every aged SKU appears somewhere, but the two beyond top5 appear ONLY inside
    # <details>, never in the top-5 table above it. `<details><summary>` (not bare
    # "<details>") is the marker: _IA_CSS's own print-media comment contains the
    # literal substring "<details>" (design rule #15's docstring), which would
    # otherwise split inside <head> rather than at the real body tag.
    details_start = out.index("<details><summary>")
    top5_html, details_html = out[:details_start], out[details_start:]
    top5_skus = {it.sku for it in truncated_report.top_items["Solo"]}
    all_skus = {it.sku for it in truncated_report.aged_items["Solo"]}
    truncated_only_skus = all_skus - top5_skus
    assert len(truncated_only_skus) == 2
    for sku in top5_skus:
        assert sku in top5_html
    for sku in truncated_only_skus:
        assert sku not in top5_html
        assert sku in details_html


# ---------------------------------------------------------------------------
# Review finding (major): the mock's 2-column .mid layout (trend chart + "By
# location" variance table side-by-side) must be reproduced, not two
# independent full-width stacked cards.
# ---------------------------------------------------------------------------
def test_trend_chart_and_variance_table_render_in_a_two_column_row(html):
    assert 'class="ia-mid"' in html
    mid_idx = html.index('class="ia-mid"')
    chart_idx = html.index("Aged share of on-hand value, by location")
    variance_idx = html.index("By location")
    buckets_idx = html.index("Aging buckets by location")
    # Both cards render inside the .ia-mid wrapper, in mock order, before the
    # next (unrelated) section begins.
    assert mid_idx < chart_idx < variance_idx < buckets_idx


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


def test_details_summary_is_never_white_on_white():
    """Found during the fix-round-1 rendered-artifact acceptance gate (viewed in a
    real browser, per report-design.md's process rule): `--accent-ink` is the
    contrast color computed for text ON the `--accent` background (table headers,
    `.fs-chip.fs-dark`, etc. -- see `_accent_ink`'s own docstring and the identical
    print-media workaround for financial_statement above). Reusing it directly for
    `.ia-section summary` -- which sits on the plain `--card` background, no
    `--accent` fill -- renders white-on-white whenever `accent_hsl` is dark (the
    DEFAULT accent, `render_report_html`'s own default param), making the "All N
    aged SKUs" collapsible toggle genuinely invisible, not merely low-contrast."""
    from app.services.report.report_html import _IA_CSS

    assert "var(--accent-ink)" not in _IA_CSS
    assert ".ia-section summary" in _IA_CSS
    assert "color: var(--ink)" in _IA_CSS.split(".ia-section summary", 1)[1].split("}", 1)[0]


def test_print_media_unclips_the_collapsible_aged_list():
    from app.services.report.report_html import _IA_CSS

    assert "@media print" in _IA_CSS
    print_block = _IA_CSS.split("@media print", 1)[1]
    assert "details" in print_block


def test_render_report_html_deterministic(spec):
    assert render_report_html(spec) == render_report_html(spec)


# ---------------------------------------------------------------------------
# Report head (spec §A1: "report head (title `Inventory Aging — Week of {snapshot
# date}`, sub-line, meta block incl. 'no model generated a figure')"). Found at the
# readiness gate by rendering the fixture and comparing it with the mock's
# `.report-head`: the render went straight from <h1> to "Watch items" -- no sub-line,
# no meta block, and the <h1> date was ISO (2026-09-08) where the mock reads
# "8 Sep 2026". The composed Framework report was worse: its <h1> was the SERIES
# title "Inventory Aging Weekly" (the Report row's title), never the mock's head.
# ---------------------------------------------------------------------------
def test_inventory_aging_title_is_the_mocks_week_of_with_day_month_year(report):
    from app.services.report.report_html import inventory_aging_title

    assert inventory_aging_title(report) == "Inventory Aging — Week of 8 Sep 2026"


def test_report_head_is_the_first_section_with_sub_line_and_meta(report):
    sections = build_inventory_aging_sections(report, composed_at="2026-09-08T13:05:00+00:00")
    assert sections[0]["type"] == "report_head"
    head = sections[0]["model"]
    # the mock's sub-line, verbatim shape: locations · method · compared with <prior>
    assert head["sub"] == "Nova · Solace · on-hand stock aged by days since last restock · compared with 1 Sep 2026"
    assert head["snapshot"] == "2026-09-08"
    assert head["prior"] == "2026-09-01"
    assert head["composed_at"] == "2026-09-08T13:05:00+00:00"


def test_report_head_renders_sub_line_and_meta_under_the_title(report):
    from app.services.report.report_html import inventory_aging_title

    sections = build_inventory_aging_sections(report, composed_at="2026-09-08T13:05:00+00:00")
    out = render_report_html({"title": inventory_aging_title(report), "sections": sections})
    h1_end = out.index("</h1>")
    watch = out.index("Watch items")
    head_block = out[h1_end:watch]
    assert "on-hand stock aged by days since last restock · compared with 1 Sep 2026" in head_block
    assert "Snapshot <b>2026-09-08</b>" in head_block
    assert "prior <b>2026-09-01</b>" in head_block
    assert "Composed 8 Sep 2026, 13:05 UTC" in head_block
    assert "no model generated a figure" in head_block


def test_report_head_without_composed_at_still_carries_the_no_model_line(report):
    sections = build_inventory_aging_sections(report)
    out = render_report_html({"title": "x", "sections": sections})
    head_block = out[out.index("</h1>") : out.index("Watch items")]
    assert "no model generated a figure" in head_block
    assert "Composed" not in head_block


# ---------------------------------------------------------------------------
# Render-polish brief item 4: the "Largest aged positions" table never clips --
# the Item column wraps (the mock's `.desc`), every other column stays nowrap,
# and the table sits in an overflow-x:auto wrapper as a last resort.
# ---------------------------------------------------------------------------
def test_top_positions_item_column_carries_the_wrap_class(html):
    idx = html.index("Largest aged positions")
    table_end = html.index("</table>", idx)
    block = html[idx:table_end]
    assert 'class="desc"' in block


def test_top_positions_table_sits_in_the_overflow_auto_wrapper(html):
    idx = html.index("Largest aged positions")
    block = html[idx : idx + 400]
    assert 'class="tblcard"' in block


def test_top_positions_headers_are_shortened_to_fit_the_reports_real_width(html):
    """Render-fidelity fix: at the report's real in-app width (~780px) the original
    "Days since restock" / "% of location aged" headers clip the table -- the section
    sub-heading (and, earlier on the page, the Aging buckets section) already
    establishes what the days/percent columns mean, so the header text itself can
    shorten to "Days" / "% of aged" without losing meaning."""
    idx = html.index("Largest aged positions")
    thead_end = html.index("</thead>", idx)
    header_block = html[idx:thead_end]
    assert "<th>Days</th>" in header_block
    assert "<th>% of aged</th>" in header_block
    assert "Days since restock" not in header_block
    assert "% of location aged" not in header_block


def test_ia_css_desc_column_wraps_min_max_width_other_cells_stay_nowrap():
    from app.services.report.report_html import _IA_CSS

    assert ".tblcard .desc" in _IA_CSS
    desc_rule = _IA_CSS.split(".tblcard .desc", 1)[1].split("}", 1)[0]
    assert "white-space: normal !important" in desc_rule
    assert "min-width: 220px" in desc_rule
    assert "max-width: 360px" in desc_rule
    # the plain `td` rule (every OTHER column) stays nowrap -- `.desc` is the only
    # override.
    td_rule = _IA_CSS.split(".tblcard td {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in td_rule


def test_ia_css_tblcard_scrolls_horizontally_as_a_last_resort():
    from app.services.report.report_html import _IA_CSS

    chart_tblcard_rule = _IA_CSS.split(".chart, .tblcard {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto" in chart_tblcard_rule


# ---------------------------------------------------------------------------
# Render-polish brief item 5: KPI card delta + label render on one line, as the
# mock ("▲ +$867.8K · +4.0% vs prior week"), sub-detail line beneath.
# ---------------------------------------------------------------------------
def test_kpi_delta_money_and_pct_share_one_bold_span_with_label_on_the_same_line(html):
    """Render-fidelity fix: "vs prior week" must live INSIDE the same <b> element as
    the delta (not as a sibling text node in the surrounding flex `.d` div) -- at the
    report's real width, `.d`'s `flex-wrap: wrap` was splitting the delta and its
    "vs prior week" label onto two separate lines."""
    import re

    m = re.search(r'<div class="d"><b class="(fav|unf)">(▲|▼) [^<]+ · [^<]+% vs prior week</b></div>', html)
    assert m is not None
    # the OLD structure (label as a sibling of </b>) must be gone
    assert re.search(r"</b> vs prior week</div>", html) is None


def test_kpi_aged_share_delta_and_vs_prior_pct_are_also_one_element(html, report):
    """The aged-share card's delta variant ("vs {prior %}" rather than "vs prior
    week") is subject to the identical flex-wrap split -- same fix, same structural
    assertion, on the OTHER `_ia_kpi_delta_html` branch (``kpi.delta_pct is None``)."""
    import re

    aged_share = next(k for k in report.kpis if k.key == "aged_share")
    assert aged_share.delta_pct is None  # sanity on the fixture contract
    m = re.search(r'<div class="d"><b class="(fav|unf)">(▲|▼) [^<]+ pts vs [^<]+%</b></div>', html)
    assert m is not None


def test_kpi_sub_detail_renders_beneath_the_delta_line(html, report):
    kpi = report.kpis[0]
    idx = html.index('class="kpi"')
    block = html[idx : html.index("</div>", html.index('class="s"', idx))]
    d_idx = block.index('class="d"')
    s_idx = block.index('class="s"')
    assert d_idx < s_idx  # sub-detail div comes AFTER the delta div, never merged into it
    assert kpi.sub_detail in block


# ---------------------------------------------------------------------------
# Render-polish brief item 6: "Sources & method" renders as the mock's labelled
# grid (Source / Snapshots used / Age (full width) / Queries / Integrity) inside
# THIS module's own .ia-section styling, not the shared generic provenance
# footer every other report type still uses.
# ---------------------------------------------------------------------------
def test_sources_and_method_renders_the_mocks_labelled_grid(html):
    idx = html.index("Sources &amp; method")
    tail = html[idx : idx + 2000]
    assert 'class="ia-prov"' in tail
    for label in ("Source", "Snapshots used", "Age", "Queries", "Integrity"):
        assert f"<b>{label}</b>" in tail
    assert "no model generated a figure" in tail


def test_sources_and_method_integrity_cell_has_no_stray_semicolon_period(html):
    """Render-fidelity fix: each entry in ``prov.integrity_checks`` already ends with
    its own terminal period (see INTEGRITY_CHECKS in inventory_aging.py) -- joining
    them with "; " and then appending "; no model generated a figure." produced a
    stray ".; " sequence (e.g. "...on-hand value.; The all-locations row...
    re-query.; no model..."). Sentences join with a single space instead, each
    already carrying its own period."""
    idx = html.index("<b>Integrity</b>")
    end = html.index("</div>", idx)
    cell = html[idx:end]
    assert ".;" not in cell
    assert "No model generated a figure." in cell or "no model generated a figure." in cell


def test_sources_and_method_age_row_spans_full_width(html):
    idx = html.index("<b>Age</b>")
    # backtrack to the wrapping <div ...> for this row -- it must carry class="full"
    div_start = html.rindex("<div", 0, idx)
    row = html[div_start : html.index(">", div_start) + 1]
    assert 'class="full"' in row


def test_sources_and_method_lives_inside_ia_section_not_the_plain_prov_footer(html):
    idx = html.index("Sources &amp; method")
    section_start = html.rfind('<div class="ia-section">', 0, idx)
    assert section_start != -1
    # the OLD plain-footer renderer's own class (`.prov`, no `ia-` prefix) never
    # appears anywhere in the output for an inventory_aging report.
    assert 'class="prov"' not in html


def test_sources_and_method_never_duplicates_even_when_provenance_kwarg_is_also_passed(spec, report):
    """Production compose/refresh call sites still pass
    ``build_inventory_aging_provenance(report.provenance)`` as the generic
    top-level ``provenance=`` kwarg (unmodified call sites, out of this brief's
    scope) -- the section-embedded grid must be the ONLY "Sources & method"
    block that renders, never a second copy from the old footer path."""
    prov = build_inventory_aging_provenance(report.provenance)
    out = render_report_html(spec, provenance=prov)
    assert out.count("Sources &amp; method") == 1


def test_report_head_model_is_json_native(report):
    """spec_json is persisted as JSONB (compose script's _json_safe) and re-rendered
    from it by scripts/backfill_report_html.py -- the head model must already be
    plain strings so a round trip through JSON renders byte-identically."""
    import json

    sections = build_inventory_aging_sections(report, composed_at="2026-09-08T13:05:00+00:00")
    head = sections[0]
    assert json.loads(json.dumps(head)) == head
