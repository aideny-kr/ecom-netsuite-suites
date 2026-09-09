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
# aged_items: the UNBOUNDED per-location aged list (review finding — top_items is
# capped at TOP_ITEMS_PER_LOCATION=5, so it cannot back a "nothing truncated" claim
# on its own; aged_items is the same sort (value desc), never sliced).
# ---------------------------------------------------------------------------
def test_aged_items_is_unbounded_not_capped_at_five():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    # Acme has 6 aged items (A-G1..A-G6) — top_items caps at 5, aged_items must not.
    assert [item.sku for item in report.aged_items["Acme"]] == [
        "A-G1",
        "A-G2",
        "A-G3",
        "A-G4",
        "A-G5",
        "A-G6",
    ]
    assert len(report.aged_items["Acme"]) == 6
    assert report.top_items["Acme"] == report.aged_items["Acme"][:5]


def test_aged_items_matches_top_items_when_fewer_than_five():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    # Globex only has 1 aged item — aged_items and top_items agree exactly.
    assert report.aged_items["Globex"] == report.top_items["Globex"]
    assert len(report.aged_items["Globex"]) == 1


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
    # ValueHit's own aged-SKU count is UNCHANGED vs prior (1 -> 1, a pure
    # price/quantity move on the same position) -- the substring this test checks
    # is "aged value" (the rule-2 watch item's own opener), not "the 90+ buckets",
    # because that phrase is exactly the zero-count-safe clause this fixture now
    # exercises (see test_watch_item_zero_sku_delta_renders_no_change_not_zero_left
    # below) -- it deliberately does NOT contain "the 90+ buckets".
    assert any("ValueHit" in t and "aged value" in t for t in texts)
    assert not any("ValueMiss" in t and "aged value" in t for t in texts)


def test_watch_item_zero_sku_delta_renders_no_change_not_zero_left():
    """Render-polish brief item 2 (the bug this fixture caught): ValueHit's aged
    SKU count is 1 both before and after (only its VALUE moved) -- the OLD wording
    picked an arbitrary direction word for a zero delta ("as 0 SKUs left the 90+
    buckets"), which is nonsensical. It must read as "no change" instead."""
    payloads, params = _threshold_fixture()
    report = ia.compute(payloads, params)
    value_hit = next(loc for loc in report.locations if loc.location == "ValueHit")
    assert value_hit.skus_90p_delta == 0  # sanity on the fixture contract
    texts = [w.text for w in report.watch_items]
    hit_text = next(t for t in texts if "ValueHit" in t and "aged value" in t)
    assert "with no change in the number of aged SKUs" in hit_text
    assert "0 SKUs" not in hit_text


@pytest.mark.parametrize(
    "delta,expected",
    [
        (0, "with no change in the number of aged SKUs"),
        (-5, "as 5 SKUs left the 90+ buckets"),
        (3, "as 3 SKUs entered the 90+ buckets"),
    ],
)
def test_sku_delta_clause_zero_negative_positive(delta, expected):
    """Direct unit coverage of the shared helper (render-polish brief item 2) --
    the three cases the brief names explicitly, independent of any fixture."""
    assert ia._sku_delta_clause(delta) == expected


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


def _mover_zero_sku_delta_fixture():
    """One location whose aged VALUE moved (so the mover highlight fires) but whose
    aged SKU COUNT did not (91-180's one SKU just got more expensive) -- render-
    polish brief item 2's zero-count wording, exercised on the HIGHLIGHTS mover
    clause specifically (the watch-item test above covers the same rule on watch
    items)."""
    items = [
        _item("Only", "O-C1", 10, 5000, 50),
        _item("Only", "O-G1", 100, 9000, 5),  # single aged SKU, value moved vs prior
    ]
    prior = [
        _prior_row(
            "Only", value=13000, value_90p=8000, value_180p=0, skus=2, skus_90p=1, skus_180p=0, qty=55, qty_90p=5
        )
    ]
    trend = [_trend_row("Only", SNAPSHOT - timedelta(weeks=w), 14000, 9000, 64.3) for w in (1, 0)]
    meta = [
        {
            "location": "Only",
            "first_snapshot_date": (SNAPSHOT - timedelta(days=90)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 90,
        }
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": ["Only"], "compare_days": 7, "trend_weeks": 2}
    return payloads, params


def test_highlight_mover_zero_sku_delta_renders_no_change_not_zero_entering():
    payloads, params = _mover_zero_sku_delta_fixture()
    report = ia.compute(payloads, params)
    only = report.locations[0]
    assert only.skus_90p_delta == 0  # sanity on the fixture contract
    assert only.aged90_value_delta != 0  # sanity: the mover rule still fires
    mover_text = next(h.text for h in report.highlights if "Only" in h.text and "aged value" in h.text)
    assert "with no change in the number of aged SKUs" in mover_text
    assert "0 SKUs" not in mover_text


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


def test_narrative_paragraph1_snapshot_date_uses_the_mocks_long_form_not_iso():
    """Render-fidelity fix: the mock reads "on the 8 Sep 2026 snapshot", not the ISO
    form "2026-09-08" -- SNAPSHOT is date(2026, 9, 8) (module-level constant above),
    so this pins the exact mock wording, not just "no ISO digits anywhere"."""
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)
    assert "on the 8 Sep 2026 snapshot" in report.narrative.paragraph_1
    assert SNAPSHOT.isoformat() not in report.narrative.paragraph_1


# ---------------------------------------------------------------------------
# Narrative paragraph 2 -- location-naming logic (render-polish brief item 3):
# "improved the most" names the location with the largest FAVOURABLE aged-value
# move, "moved the other way" names the location with the largest UNFAVOURABLE
# share move -- each drops to "No location improved/worsened this week." when
# nothing qualifies, and the second clause drops when it would repeat the first
# clause's own location name. Every fixture here uses two items per location
# (one current-bucket, one 91-180 aged) so on_hand/aged90 values are exact and
# every percentage below divides evenly (no rounding surprises to account for).
# ---------------------------------------------------------------------------
def _narrative_loc_items(location, current_value, aged_value):
    return [
        _item(location, f"{location}-CUR", 10, current_value, 10),
        _item(location, f"{location}-AGD", 100, aged_value, 5),
    ]


def _narrative_fixture(loc_specs, *, prior_skus_90p_by_location: dict[str, int] | None = None):
    """``loc_specs``: ``(location, current_value, aged_value, prior_value,
    prior_aged90)`` 5-tuples. One current + one aged item per location (see
    ``_narrative_loc_items``), a matching prior row, a 2-point trend, and
    per-location meta -- the minimum ``compute()`` needs to exercise paragraph_2's
    location-naming logic without pulling in bucket/top-item concerns this group
    of tests isn't about. ``prior_skus_90p_by_location`` overrides the prior
    aged-SKU count for a named location (default 1, matching the fixture's fixed
    one-aged-item-per-location shape, i.e. a zero SKU delta) -- pass it to give a
    location a nonzero ``skus_90p_delta`` for tests that need the real "N SKUs
    left/entered ..." clause rather than the zero-delta branch."""
    items: list[dict] = []
    prior: list[dict] = []
    trend: list[dict] = []
    meta: list[dict] = []
    locations: list[str] = []
    for location, current_value, aged_value, prior_value, prior_aged90 in loc_specs:
        locations.append(location)
        items += _narrative_loc_items(location, current_value, aged_value)
        prior.append(
            _prior_row(
                location,
                value=prior_value,
                value_90p=prior_aged90,
                value_180p=0,
                skus=2,
                skus_90p=(prior_skus_90p_by_location or {}).get(location, 1),
                skus_180p=0,
                qty=100,
                qty_90p=10,
            )
        )
        total = current_value + aged_value
        for weeks_ago in (1, 0):
            pct_90p = round(aged_value / total * 100, 1) if total else 0.0
            trend.append(_trend_row(location, SNAPSHOT - timedelta(weeks=weeks_ago), total, aged_value, pct_90p))
        meta.append(
            {
                "location": location,
                "first_snapshot_date": (SNAPSHOT - timedelta(days=90)).isoformat(),
                "last_snapshot_date": SNAPSHOT.isoformat(),
                "snapshot_count": 90,
            }
        )
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": locations, "compare_days": 7, "trend_weeks": 2}
    return payloads, params


def test_narrative_paragraph2_two_locations_both_worsening_no_location_improved():
    # Nova: aged value +5000 (not improving), share +5.0pts. Vex: aged value
    # +10000 (not improving), share +20.0pts (the larger move -> most_worsened).
    # Neither location's aged value fell -> "No location improved this week."
    payloads, params = _narrative_fixture(
        [
            ("Nova", 80000, 20000, 100000, 15000),
            ("Vex", 30000, 20000, 50000, 10000),
        ]
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "No location improved this week." in p2
    assert "Vex moved the other way" in p2
    assert "Nova moved the other way" not in p2
    assert "improved the most" not in p2
    # "carries X% of the on-hand value" still names the largest-BY-VALUE location
    # (Nova, $100K on-hand vs Vex's $50K) regardless of who moved.
    assert "Nova carries" in p2


def test_narrative_paragraph2_one_improving_one_worsening_both_named():
    # Aphex: aged value -5000 (the only improver) -> "improved the most". Byte:
    # share +20.0pts (the only worsener, Aphex's own share delta is -10pts, not
    # a worsening candidate) -> "moved the other way". Aphex also happens to be
    # the largest-by-value location (50000 vs Byte's 40000), i.e. the lead --
    # so (fix round 2) its "carries X%" and "improved the most" clauses combine
    # into one "and"-joined sentence rather than two separate ones; Byte is a
    # distinct location so its clause is unaffected.
    payloads, params = _narrative_fixture(
        [
            ("Aphex", 40000, 10000, 50000, 15000),
            ("Byte", 20000, 20000, 40000, 12000),
        ]
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "Aphex carries" in p2
    assert "and improved the most" in p2
    assert "Byte moved the other way" in p2
    assert "No location improved this week." not in p2
    assert "No location worsened this week." not in p2


def test_narrative_paragraph2_lead_and_improved_same_location_combined_with_and():
    # Fix-round-2 regression (review finding): when the value leader ("carries
    # X% of the on-hand value") is ALSO the biggest improver, the two clauses
    # must combine into one sentence with "and" -- matching the mock's real-data
    # sentence "Dimerco carries 89.7% of the on-hand value and improved the
    # most: ..." -- instead of two back-to-back sentences that repeat the same
    # location's name ("Dimerco carries ... Dimerco improved ..."). Reproduces
    # the finding's own fixture almost verbatim: Dimerco is both the larger
    # on-hand-value location AND the sole improver; Panurgy is the sole
    # worsener, a distinct location, so its clause is unaffected.
    payloads, params = _narrative_fixture(
        [
            ("Dimerco", 300000, 20000, 350000, 30000),
            ("Panurgy", 40000, 10000, 60000, 4000),
        ]
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "Dimerco carries" in p2
    assert "and improved the most: aged value fell" in p2
    assert "Dimerco carries" not in p2.split("and improved the most", 1)[1]
    # No back-to-back "Dimerco ... Dimerco" as two separate sentences.
    assert "on-hand value. Dimerco improved" not in p2
    assert "Panurgy moved the other way" in p2


def test_narrative_paragraph2_lead_and_improver_are_different_locations_two_sentences():
    # Review finding on the polish pass: every paragraph-2 test exercised the
    # "no improver" or the combined lead==improver branch; the plain two-sentence
    # form -- value leader in one sentence, a DIFFERENT location improving the
    # most in the next -- had no test. Big is the largest on-hand value and does
    # not improve (aged value +2000); Small is the sole improver (aged -10000).
    payloads, params = _narrative_fixture(
        [
            ("Big", 300000, 30000, 290000, 28000),
            ("Small", 50000, 5000, 52000, 15000),
        ]
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "Big carries" in p2
    assert "Small improved the most:" in p2
    assert "and improved the most" not in p2
    assert "Big improved" not in p2
    assert "No location improved this week." not in p2


def test_narrative_paragraph2_improved_clause_uses_the_aged_buckets_wording():
    # Regression: paragraph 2's "improved the most" clause must read "... the
    # aged buckets" (the pre-existing narrative wording, and the mock's literal
    # reference sentence "... as 23 SKUs left the aged buckets"), NOT "the 90+
    # buckets" -- the wording the watch-items/highlights call sites use for
    # ``_sku_delta_clause``. Reusing that shared helper for item 3's rewrite
    # silently switched paragraph 2 to the wrong noun; the shipped Nova/Solace
    # fixture never caught it because it happens to hit the zero-delta branch
    # (`"with no change in the number of aged SKUs"`), which carries no bucket
    # noun at all. Give Aphex (the only improver) a nonzero SKU delta --
    # prior skus_90p=3 vs the fixture's fixed current aged90_skus=1 -> delta=-2
    # -- so the real "N SKUs left ..." branch renders. Aphex is also the lead
    # (largest on-hand value), so (fix round 2) its clause reads "... and
    # improved the most: ..." rather than a separate "Aphex improved the
    # most" sentence -- the wording assertion below only cares about the
    # bucket noun, which is unaffected by that combination.
    payloads, params = _narrative_fixture(
        [
            ("Aphex", 40000, 10000, 50000, 15000),
            ("Byte", 20000, 20000, 40000, 12000),
        ],
        prior_skus_90p_by_location={"Aphex": 3},
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "and improved the most" in p2
    assert "2 SKUs left the aged buckets" in p2
    assert "the 90+ buckets" not in p2


def test_narrative_paragraph2_three_locations_one_flat_only_two_qualify():
    # Faller improves (aged value -10000), Riser worsens (share +20.0pts),
    # Flatly is exactly flat (delta_value 0, delta_pts 0) -- neither an improver
    # nor a worsener, so it must not headline either clause even though it's a
    # real third location in the report. Faller is also the lead (largest
    # on-hand value, 100000), so (fix round 2) it gets the combined "carries
    # X% ... and improved the most" sentence rather than two separate ones.
    payloads, params = _narrative_fixture(
        [
            ("Faller", 90000, 10000, 100000, 20000),
            ("Riser", 30000, 20000, 50000, 10000),
            ("Flatly", 24000, 6000, 30000, 6000),
        ]
    )
    report = ia.compute(payloads, params)
    p2 = report.narrative.paragraph_2
    assert "Faller carries" in p2
    assert "and improved the most" in p2
    assert "Riser moved the other way" in p2
    assert "Flatly improved the most" not in p2
    assert "Flatly moved the other way" not in p2
    assert "No location improved this week." not in p2
    assert "No location worsened this week." not in p2


def test_narrative_paragraph2_same_location_both_extremes_drops_the_worsened_clause():
    # Solo is the ONLY location, and its own numbers make it simultaneously the
    # largest favourable aged-value move (value fell) AND the largest
    # unfavourable share move (share rose, because total on-hand collapsed much
    # harder than the aged value did) -- the same location can't headline both
    # clauses without repeating its own name, so the second ("moved the other
    # way") clause drops entirely (gate fix #9: NOT to the generic "No location
    # worsened" line -- a location DID worsen this week, Solo itself, so that
    # sentence would be false; the correct behaviour is to say nothing about
    # worsening at all) while the first still names Solo. Solo is trivially
    # also the lead (the only location), so (fix round 2) its "carries X%"
    # clause combines with "improved the most" via "and" rather than two
    # separate sentences.
    payloads, params = _narrative_fixture([("Solo", 7000, 3000, 50000, 4000)])
    report = ia.compute(payloads, params)
    solo = report.locations[0]
    assert solo.aged90_value_delta < 0  # sanity: Solo is the improver
    assert solo.aged90_share_delta_pts > 0  # sanity: Solo is ALSO the worsener
    p2 = report.narrative.paragraph_2
    assert "Solo carries" in p2
    assert "and improved the most" in p2
    # Gate fix #9: a location (Solo) DID worsen this week -- "No location
    # worsened this week" would be false and must never print here.
    assert "No location worsened this week." not in p2
    assert "Solo moved the other way" not in p2
    # Not two back-to-back sentences repeating the location's name.
    assert "on-hand value. Solo improved" not in p2
    # Dropping the worsened clause entirely must not leave a stray double space
    # or a sentence starting mid-word where it used to sit.
    assert "  " not in p2
    assert "Solo holds the highest" in p2


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


def test_build_sources_rejects_duplicate_locations():
    """Gate fix: a duplicate location must be refused here (before any SQL is even
    built), not silently deduped downstream — deduping inside compute()'s
    location-keyed dict would collapse the duplicate but the All-locations summary's
    own `for loc in locations` sum (see _all_locations_summary) would still iterate
    the duplicate entry twice, double-counting that location into the totals."""
    with pytest.raises(ValueError, match="duplicate location: Dimerco"):
        ia.build_sources({"locations": ["Dimerco", "Dimerco"]})


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


def test_compute_also_rejects_duplicate_locations():
    """compute() uses the same validated tuple as build_sources — defense in depth
    against a tampered/drifted recipe that reached compute() directly with a
    duplicated locations list (see test_build_sources_rejects_duplicate_locations)."""
    payloads, _ = _threshold_fixture()
    with pytest.raises(ValueError, match="duplicate location: Acme"):
        ia.compute(payloads, {"locations": ["Acme", "Acme"]})


# ---------------------------------------------------------------------------
# r_trend day_rn must rank DISTINCT dates, not per-SKU rows (review finding)
#
# The old `ranked` CTE computed `day_rn` as
# ROW_NUMBER() OVER (PARTITION BY s.location ORDER BY s.snapshot_date DESC)
# directly over the raw per-SKU-per-day rows. ROW_NUMBER never ties, so every
# SKU sharing the same snapshot_date got a distinct, sequential day_rn:
#   (1) `day_rn <= 7 * trend_weeks` capped by ROW count, not DAY count -- with
#       N SKUs/location/day, only ~7*trend_weeks/N days of data survived.
#   (2) `GROUP BY location, snapshot_date, day_rn` became a no-op aggregation
#       (day_rn already unique per row within a location), so `per_day` never
#       actually summed multiple SKUs on the same date -- each row was really
#       one SKU's own inventory_amount mislabeled as a location-wide total.
# The fix ranks DISTINCT (location, snapshot_date) pairs first, then joins
# that day_rn onto every per-SKU row sharing that date.
# ---------------------------------------------------------------------------
def test_r_trend_day_rn_is_ranked_over_distinct_dates_not_per_sku_rows():
    sources = ia.build_sources({"locations": ["Acme"]})
    query = sources["r_trend"]["params"]["query"]
    # day_rn must come from a CTE over DISTINCT (location, snapshot_date)
    # pairs, not be computed inline over the raw per-SKU-per-day rows.
    assert "SELECT DISTINCT" in query
    # the per-SKU rows must be JOINED to that day_rn (not compute their own
    # ROW_NUMBER partitioned only by location over raw per-SKU rows).
    assert "ROW_NUMBER() OVER (PARTITION BY s.location ORDER BY s.snapshot_date DESC)" not in query


def test_r_trend_sums_multiple_skus_sharing_a_date_into_one_trend_point():
    """Execution-shaped regression for the day_rn bug: with 2 SKUs sharing the
    same (location, snapshot_date), the query's own per_day GROUP BY key must
    be able to collapse them to one row per date. This can't run against real
    BigQuery here, so we assert the SQL groups by (location, snapshot_date)
    with a day_rn that is IDENTICAL for every SKU on that date -- i.e. day_rn
    is selected from the distinct-dates CTE, not computed per source row."""
    sources = ia.build_sources({"locations": ["Acme"]})
    query = sources["r_trend"]["params"]["query"]
    # the distinct-dates CTE ranks by (location, snapshot_date) only -- no sku
    distinct_idx = query.index("SELECT DISTINCT")
    # the DISTINCT projection must be location + snapshot_date, never sku,
    # so two SKUs on the same date collapse to one (location, date) pair.
    distinct_line = query[distinct_idx : distinct_idx + 80]
    assert "sku" not in distinct_line.lower()


# ---------------------------------------------------------------------------
# rows_from_table_payload / json_safe (refresh-support follow-up) -- shared
# converters used by refresh_service/playbooks (payload -> compute() rows) and
# by compose_inventory_aging/report_service (compute() result -> JSONB-safe).
# ---------------------------------------------------------------------------
def test_rows_from_table_payload_zips_columns_and_positional_rows():
    payload = {"columns": ["location", "sku"], "rows": [["Acme", "A-1"], ["Globex", "G-1"]]}
    assert ia.rows_from_table_payload(payload) == [
        {"location": "Acme", "sku": "A-1"},
        {"location": "Globex", "sku": "G-1"},
    ]


def test_rows_from_table_payload_tolerates_missing_or_malformed_payload():
    assert ia.rows_from_table_payload({}) == []
    assert ia.rows_from_table_payload({"columns": ["a"], "rows": None}) == []
    assert ia.rows_from_table_payload(None) == []  # type: ignore[arg-type]


def test_rows_from_table_payload_fails_closed_on_a_truncated_source():
    """Gate fix #8: a BigQuery source's raw tool result reporting truncated=True
    (its own row extraction cap silently dropped rows before compute() ever saw
    them) must never pass through as if it were a complete row set -- a KPI,
    bucket total, or top-N list built from a partial extraction with no
    truncation indicator anywhere in the render is silently wrong."""
    payload = {"columns": ["location", "sku"], "rows": [["Acme", "A-1"]], "truncated": True}
    with pytest.raises(ia.SourceTruncated):
        ia.rows_from_table_payload(payload)


def test_rows_from_table_payload_truncated_error_names_the_source_when_given():
    with pytest.raises(ia.SourceTruncated) as exc:
        ia.rows_from_table_payload({"truncated": True}, rid="r_items")
    assert "r_items" in str(exc.value)


def test_json_safe_converts_decimal_date_and_dataclasses_never_through_float():
    payloads, params = _full_fixture()
    report = ia.compute(payloads, params)

    safe = ia.json_safe(report)

    assert isinstance(safe, dict)
    _assert_no_float(safe)

    def _walk_for_raw(value):
        assert not isinstance(value, Decimal)
        assert not isinstance(value, date)
        assert not dataclasses.is_dataclass(value) or isinstance(value, type)
        if isinstance(value, dict):
            for v in value.values():
                _walk_for_raw(v)
        elif isinstance(value, list):
            for v in value:
                _walk_for_raw(v)

    _walk_for_raw(safe)
    # round-trips through real JSON (the actual JSONB-safety proof)
    import json

    json.dumps(safe)
