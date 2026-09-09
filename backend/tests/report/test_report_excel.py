"""Task 3 (Slice 1) — Excel workbook builder for the inventory-aging playbook.

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A3. Two seams:

- ``build_workbook(sheets: list[SheetSpec]) -> io.BytesIO`` (evidence_service.py) —
  a NEW generic multi-sheet workbook writer: sanitises every string cell with the
  existing ``escape_csv_injection`` (OWASP CSV-injection mitigation), freezes the
  header row, sets autofilter, writes numbers as numbers and dates as dates, and
  caps sheet names at Excel's 31-char limit. It does NOT touch
  ``generate_section_excel``/``generate_excel`` — the evidence pack's own tests
  (``test_evidence_service.py``) stay byte-identical/green because that code path
  is untouched by this task.
- ``build_inventory_aging_workbook(report: AgingReport) -> io.BytesIO``
  (report_excel.py) — turns a computed ``AgingReport`` (Task 1) into the seven
  sheets spec'd in §A3: Summary, Buckets, one sheet per location, Aged 90+,
  Method.

Fixture is entirely SYNTHETIC (fake locations/SKUs/dollar values) — per the plan's
Global Constraints and report-design.md, the mock's real tenant numbers are never
pasted into a test. Three locations are used deliberately so the "seven sheets"
requirement (Summary + Buckets + 3 location sheets + Aged 90+ + Method) is the
same shape ``DEFAULT_LOCATIONS`` produces in production.

Per spec §A3 the per-location sheets carry "every SKU" (r_items returns "every
on-hand SKU per location" per §A1) — so they are built from
``report.all_items[location]``, the full unbounded per-location list covering
every bucket (review finding: an earlier version of this module built the
per-location sheets from ``report.aged_items`` instead, a Task-1 gap now
closed by adding ``AgingReport.all_items``). "Aged 90+" stays the smaller,
aged-only (91-180 / 180+) union across locations, built from
``report.aged_items`` as before — a distinct, narrower sheet, not the same
rows repeated.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.services.reconciliation.evidence_service import build_workbook
from app.services.report.inventory_aging import RESULT_IDS, compute
from app.services.report.report_excel import (
    AGED_SHEET_NAME,
    BUCKETS_SHEET_NAME,
    METHOD_SHEET_NAME,
    SUMMARY_SHEET_NAME,
    build_inventory_aging_workbook,
)

SNAPSHOT = date(2026, 9, 8)
LOCATIONS = ("Nova", "Solace", "Ember")


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


def _fixture():
    """Nova: 2 current + 2 aged. Solace: 1 current + 1 aged (SKU description carries
    a leading '=' on purpose, to exercise the CSV-injection escape end-to-end
    through the real report pipeline, not just build_workbook in isolation).
    Ember: 1 current + 0 aged (an empty per-location sheet is real, not an error)."""
    items = [
        _item("Nova", "NOV-A1", 10, 50000, 100, desc="Alpha Widget", category="Widgets"),
        _item("Nova", "NOV-A2", 120, 80000, 20, desc="Delta Widget", category="Widgets"),
        _item("Nova", "NOV-A3", 200, 60000, 10, desc="Epsilon Widget", category="Widgets"),
        _item("Solace", "SOL-B1", 5, 12000, 40, desc="Nu Gadget", category="Gadgets"),
        _item("Solace", "SOL-B2", 140, 40000, 15, desc="=CMD|'/c calc'!A1", category="Gadgets"),
        _item("Ember", "EMB-C1", 8, 9000, 12, desc="Iota Gizmo", category="Gizmos"),
    ]
    prior = [
        _prior_row(
            "Nova",
            value=200000,
            value_90p=70000,
            value_180p=20000,
            skus=3,
            skus_90p=2,
            skus_180p=1,
            qty=140,
            qty_90p=30,
        ),
        _prior_row(
            "Solace", value=55000, value_90p=38000, value_180p=0, skus=2, skus_90p=1, skus_180p=0, qty=60, qty_90p=15
        ),
        _prior_row("Ember", value=8000, value_90p=0, value_180p=0, skus=1, skus_90p=0, skus_180p=0, qty=10, qty_90p=0),
    ]
    weekly = {
        "Nova": [(1, 200000, 70000), (0, 190000, 80000)],
        "Solace": [(1, 55000, 38000), (0, 52000, 40000)],
        "Ember": [(1, 8000, 0), (0, 9000, 0)],
    }
    trend = []
    for loc, points in weekly.items():
        for weeks_ago, total_value, value_90p in points:
            d = SNAPSHOT - timedelta(weeks=weeks_ago)
            pct_90p = round(value_90p / total_value * 100, 1) if total_value else 0
            trend.append(_trend_row(loc, d, total_value, value_90p, pct_90p))
    meta = [
        {
            "location": loc,
            "first_snapshot_date": (SNAPSHOT - timedelta(days=120)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 120,
        }
        for loc in LOCATIONS
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": list(LOCATIONS), "compare_days": 7, "trend_weeks": 2}
    return payloads, params


@pytest.fixture
def report():
    payloads, params = _fixture()
    return compute(payloads, params)


@pytest.fixture
def wb(report):
    buf = build_inventory_aging_workbook(report)
    return load_workbook(buf)


# ---------------------------------------------------------------------------
# Gate fix #6/#4: build_inventory_aging_workbook must accept the JSON-safe dict
# form a report's persisted spec_json already carries -- the SAME single boundary-
# conversion pattern report_html.build_inventory_aging_sections uses. A caller
# that builds the delivered workbook straight off the stored model (rather than a
# fresh compute()) must get byte-identical bytes.
# ---------------------------------------------------------------------------
def test_workbook_from_json_round_tripped_report_matches_fresh_compute(report):
    import json

    from app.services.report.inventory_aging import json_safe

    stored = json.loads(json.dumps(json_safe(report)))
    fresh_bytes = build_inventory_aging_workbook(report).getvalue()
    stored_bytes = build_inventory_aging_workbook(stored).getvalue()
    assert stored_bytes == fresh_bytes


# ---------------------------------------------------------------------------
# Sheet count / order / names (spec §A3)
# ---------------------------------------------------------------------------
def test_seven_sheets_in_spec_order_and_names(wb, report):
    expected = [SUMMARY_SHEET_NAME, BUCKETS_SHEET_NAME, "Nova", "Solace", "Ember", AGED_SHEET_NAME, METHOD_SHEET_NAME]
    assert wb.sheetnames == expected
    assert len(wb.sheetnames) == 7


def test_sheet_names_capped_at_31_chars_via_build_workbook():
    buf = build_workbook(
        [{"name": "A" * 50, "headers": ["h"], "rows": [["v"]]}],
    )
    out = load_workbook(buf)
    assert len(out.sheetnames[0]) <= 31
    assert out.sheetnames[0] == "A" * 31


# ---------------------------------------------------------------------------
# Header row frozen + autofilter on every data sheet (spec §A3)
# ---------------------------------------------------------------------------
def test_header_row_frozen_and_autofilter_on_every_sheet(wb):
    for name in wb.sheetnames:
        ws = wb[name]
        assert ws.freeze_panes == "A2", f"{name} missing frozen header row"
        assert ws.auto_filter.ref, f"{name} missing autofilter"


def test_build_workbook_freezes_header_and_sets_autofilter_even_with_zero_rows():
    buf = build_workbook([{"name": "Empty", "headers": ["a", "b"], "rows": []}])
    ws = load_workbook(buf)["Empty"]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref


# ---------------------------------------------------------------------------
# CSV-injection escaping (OWASP) applied to every string cell, end-to-end
# ---------------------------------------------------------------------------
def test_formula_leading_char_prefixed_with_quote_in_location_sheet(wb):
    ws = wb["Solace"]
    descriptions = [row[1].value for row in ws.iter_rows(min_row=2)]
    assert "'=CMD|'/c calc'!A1" in descriptions


@pytest.mark.parametrize("trigger", ["=SUM(1,1)", "+1+1", "-1-1", "@SUM(1,1)"])
def test_build_workbook_prefixes_every_owasp_trigger_char(trigger):
    buf = build_workbook([{"name": "S", "headers": ["h"], "rows": [[trigger]]}])
    ws = load_workbook(buf)["S"]
    cell = ws.cell(row=2, column=1)
    assert cell.value == f"'{trigger}"
    assert cell.data_type != "f"  # never typed as a formula cell


def test_build_workbook_does_not_escape_non_string_values():
    buf = build_workbook([{"name": "S", "headers": ["h"], "rows": [[Decimal("-3.20")]]}])
    ws = load_workbook(buf)["S"]
    cell = ws.cell(row=2, column=1)
    assert cell.value == -3.20
    assert cell.data_type == "n"


# ---------------------------------------------------------------------------
# Numbers are numeric cells, dates are date cells (spec §A3 per-location columns)
# ---------------------------------------------------------------------------
def test_per_location_sheet_columns_and_cell_types(wb, report):
    ws = wb["Nova"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert header == [
        "SKU",
        "Item",
        "Category",
        "Units",
        "Value",
        "Days since restock",
        "Bucket",
        "Last restock",
        "Snapshot",
    ]

    row = next(ws.iter_rows(min_row=2, max_row=2))
    sku_cell, item_cell, category_cell, units_cell, value_cell, days_cell, bucket_cell, restock_cell, snapshot_cell = (
        row
    )

    assert sku_cell.data_type == "s"
    assert units_cell.data_type == "n"
    assert isinstance(units_cell.value, int)
    assert value_cell.data_type == "n"
    # openpyxl round-trips a whole-number float (Decimal -> float(80000) ==
    # 80000.0) as a plain int on reload -- the write side (_workbook_cell_value)
    # still converts Decimal -> float; this only asserts it's a NUMERIC cell,
    # not a string, matching the "numbers are numeric cells" requirement.
    assert isinstance(value_cell.value, (int, float))
    assert days_cell.data_type == "n"
    assert restock_cell.is_date
    assert snapshot_cell.is_date
    assert snapshot_cell.value == datetime(SNAPSHOT.year, SNAPSHOT.month, SNAPSHOT.day)


def test_build_workbook_writes_native_date_as_a_date_cell():
    buf = build_workbook([{"name": "S", "headers": ["d"], "rows": [[date(2026, 1, 15)]]}])
    ws = load_workbook(buf)["S"]
    cell = ws.cell(row=2, column=1)
    assert cell.is_date


def test_build_workbook_strips_tz_from_tz_aware_datetime():
    buf = build_workbook(
        [{"name": "S", "headers": ["d"], "rows": [[datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)]]}]
    )
    ws = load_workbook(buf)["S"]
    cell = ws.cell(row=2, column=1)
    assert cell.is_date
    assert cell.value.tzinfo is None


# ---------------------------------------------------------------------------
# Per-location row count matches the fixture's FULL per-location item count —
# every bucket, not just the aged (91+ day) subset (spec §A3 "every SKU";
# blocker finding: an earlier version undercounted these sheets to aged-only)
# ---------------------------------------------------------------------------
def test_per_location_sheet_row_count_matches_fixture(wb, report):
    for loc in LOCATIONS:
        ws = wb[loc]
        data_rows = list(ws.iter_rows(min_row=2))
        assert len(data_rows) == len(report.all_items[loc])


def test_per_location_sheet_row_count_exceeds_aged_only_when_current_items_exist(wb, report):
    """Nova and Solace each carry current (0-90 day) items alongside aged ones —
    the per-location sheet must be strictly bigger than the aged-only subset,
    proving the sheet is not secretly the same rows as "Aged 90+"."""
    for loc in ("Nova", "Solace"):
        assert len(report.all_items[loc]) > len(report.aged_items[loc])
        ws = wb[loc]
        assert len(list(ws.iter_rows(min_row=2))) == len(report.all_items[loc])


def test_location_sheet_includes_current_bucket_skus_not_only_aged(wb):
    """NOV-A1 (10 days -> bucket "0-30", a CURRENT item) must appear in Nova's
    sheet even though it is absent from report.aged_items — this is the exact
    gap the blocker finding identified."""
    ws = wb["Nova"]
    skus = {row[0].value for row in ws.iter_rows(min_row=2)}
    assert "NOV-A1" in skus


def test_ember_location_sheet_has_the_current_item_aged_items_alone_would_miss(wb, report):
    """Ember has exactly one on-hand SKU (EMB-C1, a CURRENT item, 0 aged) — a
    sheet built from aged_items alone would wrongly render this as empty."""
    assert report.aged_items["Ember"] == ()
    ws = wb["Ember"]
    rows = list(ws.iter_rows(min_row=2))
    assert len(rows) == 1
    assert rows[0][0].value == "EMB-C1"


# ---------------------------------------------------------------------------
# Aged 90+ sheet is the union of every location's aged rows
# ---------------------------------------------------------------------------
def test_aged_90plus_sheet_is_union_of_aged_rows(wb, report):
    ws = wb[AGED_SHEET_NAME]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert header[0] == "Location"

    rows = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
    total_aged = sum(len(report.aged_items[loc]) for loc in LOCATIONS)
    assert len(rows) == total_aged

    skus_in_sheet = {row[1] for row in rows}
    for loc in LOCATIONS:
        for item in report.aged_items[loc]:
            assert item.sku in skus_in_sheet


# ---------------------------------------------------------------------------
# Method sheet lists the four sources
# ---------------------------------------------------------------------------
def test_method_sheet_lists_four_sources(wb):
    ws = wb[METHOD_SHEET_NAME]
    ids_in_sheet = {row[0].value for row in ws.iter_rows(min_row=2)}
    # "lists the four sources" -- a superset check: the sheet may (and does)
    # carry additional provenance rows (source/age_definition/integrity checks),
    # it just must not be MISSING any of the four.
    assert set(RESULT_IDS) <= ids_in_sheet


# ---------------------------------------------------------------------------
# Summary + Buckets sheets carry the location rows (structural smoke)
# ---------------------------------------------------------------------------
def test_summary_sheet_has_a_row_per_kpi_and_per_location(wb, report):
    ws = wb[SUMMARY_SHEET_NAME]
    labels = [row[1].value for row in ws.iter_rows(min_row=2)]
    for kpi in report.kpis:
        assert kpi.label in labels
    for loc in report.locations:
        assert loc.location in labels
    assert "All locations" in labels


def test_buckets_sheet_has_a_row_per_location_per_bucket(wb, report):
    from app.services.report.inventory_aging import BUCKETS

    ws = wb[BUCKETS_SHEET_NAME]
    rows = [(row[0].value, row[1].value) for row in ws.iter_rows(min_row=2)]
    expected = {(loc.location, b) for loc in (*report.locations, report.all_locations) for b in BUCKETS}
    assert set(rows) == expected
