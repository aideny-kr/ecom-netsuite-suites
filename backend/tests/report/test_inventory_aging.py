"""Tests for backend/app/services/report/inventory_aging.py (Slice 1, Task 1).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
Part A. Fixtures below are entirely SYNTHETIC (fake locations, fake SKUs, fake
dollar values) — never the mock's real tenant numbers (report-design.md / the
plan's Global Constraints). ``compute()`` is a pure function: every fixture here
is a plain dict shaped like a BigQuery row, never a live BigQuery call.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.report import inventory_aging as ia

SNAPSHOT = date(2026, 9, 8)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _item(location, sku, days, value, qty, *, desc="Widget", category="Misc"):
    """A synthetic r_items row. ``bucket`` is deliberately wrong — compute() must
    derive the bucket from ``days`` itself (server-side, never trusted from the
    stored/replayed row) rather than trust whatever string arrives on the row."""
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
        "bucket": "WRONG-ON-PURPOSE",
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


def _full_fixture():
    """Three synthetic locations (Acme, Globex, Initech) exercising buckets, top-5,
    prior-week deltas, trend ordering, watch/highlight generation, and narrative."""
    items = [
        # Acme: 3 current + 6 aged (aged count > 5 so top-5 truncation is exercised)
        _item("Acme", "A-C1", 5, 1000, 10),
        _item("Acme", "A-C2", 45, 1500, 15),
        _item("Acme", "A-C3", 80, 500, 5),
        _item("Acme", "A-G1", 100, 6000, 60, desc="Big Widget", category="Alpha"),
        _item("Acme", "A-G2", 110, 5000, 50, desc="Big Widget 2", category="Alpha"),
        _item("Acme", "A-G3", 120, 4000, 40),
        _item("Acme", "A-G4", 150, 3000, 30),
        _item("Acme", "A-G5", 200, 2000, 20),
        _item("Acme", "A-G6", 210, 1000, 10),
        # Globex: small, flat share vs prior week
        _item("Globex", "G-C1", 10, 7000, 70),
        _item("Globex", "G-C2", 50, 1000, 10),
        _item("Globex", "G-G1", 95, 2000, 20),
        # Initech: on-hand value FELL while aged share ROSE (attribution highlight)
        _item("Initech", "I-C1", 20, 4000, 40),
        _item("Initech", "I-G1", 170, 1000, 10),
        _item("Initech", "I-G2", 185, 3000, 30),
    ]
    prior = [
        _prior_row(
            "Acme", value=22000, value_90p=19000, value_180p=2500, skus=8, skus_90p=5, skus_180p=1, qty=220, qty_90p=170
        ),
        _prior_row(
            "Globex", value=9500, value_90p=1900, value_180p=0, skus=3, skus_90p=1, skus_180p=0, qty=95, qty_90p=19
        ),
        _prior_row(
            "Initech", value=8400, value_90p=3800, value_180p=2900, skus=3, skus_90p=2, skus_180p=1, qty=84, qty_90p=38
        ),
    ]
    # 3 weekly trend points per location, deliberately UNSORTED to exercise ordering.
    trend = []
    weekly_values = {
        "Acme": [(2, 20000, 8000), (1, 22000, 9000), (0, 24000, 9000)],
        "Globex": [(2, 9000, 1700), (1, 9500, 1900), (0, 10000, 2000)],
        "Initech": [(2, 9000, 3500), (1, 8400, 3800), (0, 8000, 4000)],
    }
    for loc, points in weekly_values.items():
        for weeks_ago, total_value, value_90p in points:
            d = SNAPSHOT - timedelta(weeks=weeks_ago)
            pct_90p = round(value_90p / total_value * 100, 1)
            trend.append(_trend_row(loc, d, total_value, value_90p, pct_90p))
    # shuffle deterministically (reverse) so "oldest -> newest" is a real assertion
    trend = list(reversed(trend))

    meta = [
        {
            "location": loc,
            "first_snapshot_date": (SNAPSHOT - timedelta(days=90)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 90,
        }
        for loc in ("Acme", "Globex", "Initech")
    ]

    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": ["Acme", "Globex", "Initech"], "compare_days": 7, "trend_weeks": 3}
    return payloads, params


def _threshold_fixture():
    """Four synthetic locations dedicated to the watch-item threshold tests: one
    pair straddling the 1.0-pt share-move threshold, one pair straddling the
    $50,000 aged-value-move threshold."""
    items = [
        # ShareHit: current share 20.0%, prior share 19.0% -> delta exactly +1.0pt
        _item("ShareHit", "SH-C1", 10, 8000, 80),
        _item("ShareHit", "SH-G1", 100, 2000, 20),
        # ShareMiss: current share 20.0%, prior share 19.1% -> delta +0.9pt (below)
        _item("ShareMiss", "SM-C1", 10, 8000, 80),
        _item("ShareMiss", "SM-G1", 100, 2000, 20),
        # ValueHit: aged value 1,050,000 vs prior 1,000,000 -> delta exactly +$50,000
        # (on a $10M base so the SHARE move is only 0.5pt — isolates the value rule
        # from the share rule, which is tested separately above).
        _item("ValueHit", "VH-C1", 10, 8950000, 1),
        _item("ValueHit", "VH-G1", 100, 1050000, 1),
        # ValueMiss: aged value 1,049,999 vs prior 1,000,000 -> delta +$49,999 (below)
        _item("ValueMiss", "VM-C1", 10, 8950001, 1),
        _item("ValueMiss", "VM-G1", 100, 1049999, 1),
    ]
    prior = [
        _prior_row(
            "ShareHit", value=10000, value_90p=1900, value_180p=0, skus=2, skus_90p=1, skus_180p=0, qty=100, qty_90p=19
        ),
        _prior_row(
            "ShareMiss", value=10000, value_90p=1910, value_180p=0, skus=2, skus_90p=1, skus_180p=0, qty=100, qty_90p=19
        ),
        _prior_row(
            "ValueHit",
            value=10000000,
            value_90p=1000000,
            value_180p=0,
            skus=2,
            skus_90p=1,
            skus_180p=0,
            qty=2,
            qty_90p=1,
        ),
        _prior_row(
            "ValueMiss",
            value=10000000,
            value_90p=1000000,
            value_180p=0,
            skus=2,
            skus_90p=1,
            skus_180p=0,
            qty=2,
            qty_90p=1,
        ),
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": [], "r_meta": []}
    params = {"locations": ["ShareHit", "ShareMiss", "ValueHit", "ValueMiss"], "compare_days": 7, "trend_weeks": 9}
    return payloads, params


def _assert_no_float(value, path="report"):
    """Recursively walk a compute() result and fail if any *number* is a float —
    every money/share figure must be Decimal server-side (plan Global Constraints)."""
    if isinstance(value, float):
        pytest.fail(f"float found at {path}: {value!r}")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            _assert_no_float(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, dict):
        for k, v in value.items():
            _assert_no_float(v, f"{path}[{k!r}]")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_no_float(v, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# Bucket assignment — boundaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "days,expected",
    [
        (0, "0-30"),
        (30, "0-30"),
        (31, "31-60"),
        (60, "31-60"),
        (61, "61-90"),
        (90, "61-90"),
        (91, "91-180"),
        (180, "91-180"),
        (181, "180+"),
    ],
)
def test_bucket_for_days_boundaries(days, expected):
    assert ia.bucket_for_days(days) == expected


def test_bucket_for_days_ignores_stored_bucket_field():
    # compute() must recompute the bucket from `days`, never trust a replayed row's
    # own `bucket` string (see the ``_item`` helper's "WRONG-ON-PURPOSE" sentinel).
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    acme = next(loc for loc in report.locations if loc.location == "Acme")
    for bucket_row in acme.buckets:
        assert bucket_row.bucket in ia.BUCKETS


# ---------------------------------------------------------------------------
# share_pct — pure rounding helper (1 dp)
# ---------------------------------------------------------------------------
def test_share_pct_rounds_to_one_decimal_place():
    assert ia.share_pct(Decimal("1"), Decimal("3")) == Decimal("33.3")
    assert ia.share_pct(Decimal("2"), Decimal("3")) == Decimal("66.7")


def test_share_pct_zero_denominator_is_zero_not_a_crash():
    assert ia.share_pct(Decimal("100"), Decimal("0")) == Decimal("0.0")


# ---------------------------------------------------------------------------
# compute() — location totals / bucket sums / all-locations aggregate
# ---------------------------------------------------------------------------
def test_location_totals_equal_sum_of_its_own_buckets():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    for loc in report.locations:
        bucket_sum = sum((b.value for b in loc.buckets), Decimal("0"))
        assert loc.on_hand_value == bucket_sum
        unit_sum = sum((b.units for b in loc.buckets), 0)
        assert loc.units == unit_sum


def test_acme_on_hand_value_and_share():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    acme = next(loc for loc in report.locations if loc.location == "Acme")
    assert acme.on_hand_value == Decimal("24000")
    assert acme.units == 240
    assert acme.skus == 9
    assert acme.aged90_value == Decimal("21000")
    assert acme.aged90_share_pct == Decimal("87.5")


def test_all_locations_totals_equal_sum_of_locations():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    assert report.all_locations.on_hand_value == sum((loc.on_hand_value for loc in report.locations), Decimal("0"))
    assert report.all_locations.units == sum(loc.units for loc in report.locations)
    assert report.all_locations.skus == sum(loc.skus for loc in report.locations)
    assert report.all_locations.aged90_value == sum((loc.aged90_value for loc in report.locations), Decimal("0"))


# ---------------------------------------------------------------------------
# Prior-week deltas and Δ pts
# ---------------------------------------------------------------------------
def test_prior_week_deltas_and_delta_points():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    initech = next(loc for loc in report.locations if loc.location == "Initech")
    # on-hand fell 8400 -> 8000
    assert initech.delta_value == Decimal("-400")
    assert initech.delta_pct == Decimal("-4.8")
    # aged share rose from 45.2% (3800/8400) to 50.0% (4000/8000)
    assert initech.aged90_share_pct == Decimal("50.0")
    assert initech.aged90_share_delta_pts == Decimal("4.8")


# ---------------------------------------------------------------------------
# Trend ordering
# ---------------------------------------------------------------------------
def test_trend_points_ordered_oldest_to_newest():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    for loc in params["locations"]:
        points = report.trend[loc]
        assert len(points) == 3
        dates = [p.d for p in points]
        assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# Top-5 per location by value
# ---------------------------------------------------------------------------
def test_top5_per_location_by_value_and_share():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    acme_top = report.top_items["Acme"]
    assert [item.sku for item in acme_top] == ["A-G1", "A-G2", "A-G3", "A-G4", "A-G5"]
    assert [item.value for item in acme_top] == [
        Decimal("6000"),
        Decimal("5000"),
        Decimal("4000"),
        Decimal("3000"),
        Decimal("2000"),
    ]
    acme = next(loc for loc in report.locations if loc.location == "Acme")
    # top 5 = 20000 of 21000 aged value = 95.2%
    assert acme.top5_share_pct == Decimal("95.2")


def test_top5_never_exceeds_five_even_with_fewer_aged_items():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    # Globex only has 1 aged item
    assert len(report.top_items["Globex"]) == 1


# ---------------------------------------------------------------------------
# Watch-item threshold rules — fire exactly AT the threshold, not below it
# ---------------------------------------------------------------------------
def test_watch_item_share_threshold_fires_at_exactly_one_point_not_below():
    payloads, params = _threshold_fixture()
    report = ia.compute(payloads, params)
    texts = [w.text for w in report.watch_items]
    assert any("ShareHit" in t and "in a week:" in t for t in texts)
    assert not any("ShareMiss" in t and "in a week:" in t for t in texts)


def test_watch_item_value_threshold_fires_at_exactly_fifty_thousand_not_below():
    payloads, params = _threshold_fixture()
    report = ia.compute(payloads, params)
    texts = [w.text for w in report.watch_items]
    assert any("ValueHit" in t and "the 90+ buckets" in t for t in texts)
    assert not any("ValueMiss" in t and "the 90+ buckets" in t for t in texts)


def test_watch_items_capped_at_four():
    payloads, params = _threshold_fixture()
    report = ia.compute(payloads, params)
    assert len(report.watch_items) <= 4


# ---------------------------------------------------------------------------
# Highlight ordering — largest mover first
# ---------------------------------------------------------------------------
def test_highlight_ordering_largest_mover_first():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    texts = [h.text for h in report.highlights]
    # Acme's aged-value mover ($2,000 swing) must outrank Initech's
    # denominator-attribution highlight ($400 swing) in the SAME (dollar) units.
    mover_idx = next(i for i, t in enumerate(texts) if "Acme" in t and "driven by" in t)
    attribution_idx = next(i for i, t in enumerate(texts) if "Initech" in t and "denominator moved" in t)
    assert mover_idx < attribution_idx


# ---------------------------------------------------------------------------
# Narrative — slots filled, deterministic
# ---------------------------------------------------------------------------
def test_narrative_slots_filled_and_deterministic():
    payloads, params = _full_fixture()
    report_1 = ia.compute(payloads, params)
    report_2 = ia.compute(payloads, params)
    assert report_1.narrative == report_2.narrative
    assert report_1.narrative.paragraph_1
    assert report_1.narrative.paragraph_2
    assert "None" not in report_1.narrative.paragraph_1
    assert "None" not in report_1.narrative.paragraph_2
    assert "Acme" in report_1.narrative.paragraph_1 or "Acme" in report_1.narrative.paragraph_2


# ---------------------------------------------------------------------------
# Decimal everywhere
# ---------------------------------------------------------------------------
def test_no_float_anywhere_in_the_computed_report():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    _assert_no_float(report)


# ---------------------------------------------------------------------------
# build_sources — location validation + SQL-injection-shaped rejection
# ---------------------------------------------------------------------------
def test_build_sources_returns_the_four_recipe_sources():
    sources = ia.build_sources({"locations": ["Acme", "Globex"]})
    assert set(sources) == {"r_items", "r_prior", "r_trend", "r_meta"}
    for rid, source in sources.items():
        assert source["tool"] == "bigquery_sql"
        assert source["connection_id"] is None
        assert "query" in source["params"]
        assert "Acme" in source["params"]["query"]
        assert "Globex" in source["params"]["query"]


def test_build_sources_rejects_unknown_location_shape():
    with pytest.raises(ValueError):
        ia.build_sources({"locations": ["Acme", "???"]})


def test_build_sources_rejects_location_containing_a_quote():
    with pytest.raises(ValueError):
        ia.build_sources({"locations": ["Acme", "O'Brien Depot"]})


def test_build_sources_rejects_empty_locations():
    with pytest.raises(ValueError):
        ia.build_sources({"locations": []})


# ---------------------------------------------------------------------------
# r_prior/r_trend last-restock freshness (review finding: negative `days`)
#
# Regression for a review finding: r_prior/r_trend's `last_restock` join used
# to be a bare (location, sku) match with NO date bound, so a SKU restocked
# between the historical comparison date and "now" got a last_restock_date
# LATER than the row being compared — a negative `days` that a strict `> 90`
# / `> 180` COUNTIF/SUM silently drops, understating every prior/trend aged
# aggregate. The fix computes `last_restock_date` as a per-row cumulative-max
# window (as of THAT row's own snapshot_date) and joins on snapshot_date too,
# so every row's `days` is always non-negative by construction.
# ---------------------------------------------------------------------------
def test_r_prior_last_restock_is_bound_to_each_rows_own_snapshot_date():
    sources = ia.build_sources({"locations": ["Acme"]})
    query = sources["r_prior"]["params"]["query"]
    # the old bug signature: a location+sku-only join with no date component
    assert "USING (location, sku)" not in query
    # last_restock must be joined on the row's OWN snapshot_date, not a single
    # scalar "as of latest" literal
    assert "r.snapshot_date = sn.snapshot_date" in query
    # last_restock itself must be a per-row cumulative max, not a single
    # GROUP BY MAX(...) per (location, sku) that ignores the row's own date
    assert "MAX(restock_date) OVER (PARTITION BY location, sku ORDER BY snapshot_date)" in query


def test_r_trend_last_restock_is_bound_to_each_rows_own_snapshot_date():
    sources = ia.build_sources({"locations": ["Acme"]})
    query = sources["r_trend"]["params"]["query"]
    assert "USING (location, sku)" not in query
    assert "lr.snapshot_date = rk.snapshot_date" in query
    assert "MAX(restock_date) OVER (PARTITION BY location, sku ORDER BY snapshot_date)" in query


def test_r_items_last_restock_is_also_bound_to_snapshot_date():
    """r_items wasn't the buggy source (it only ever compares against the single
    latest snapshot), but it shares `_last_restock_ctes` — assert its join was
    updated consistently rather than left on the old USING(...) shape."""
    sources = ia.build_sources({"locations": ["Acme"]})
    query = sources["r_items"]["params"]["query"]
    assert "USING (location, sku)" not in query
    assert "r.snapshot_date = c.snapshot_date" in query


def test_compute_also_rejects_unknown_location_shape():
    payloads, _ = _threshold_fixture()
    with pytest.raises(ValueError):
        ia.compute(payloads, {"locations": ["Acme", "Bad;Loc"]})
