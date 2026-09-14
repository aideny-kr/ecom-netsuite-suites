"""Inventory Aging Weekly — Excel workbook (Slice 1, Task 3).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
Part A3. One entry point: ``build_inventory_aging_workbook(report) -> io.BytesIO``,
turning a computed ``AgingReport`` (Task 1's ``inventory_aging.compute()``) into
the seven sheets spec'd there, in order: Summary, Buckets, one sheet per location,
Aged 90+, Method. Built on the generic ``build_workbook`` (evidence_service.py)
so every string cell gets the same OWASP CSV-injection escaping as the
reconciliation evidence pack, every sheet freezes its header row and carries an
autofilter, and sheet names are capped at Excel's 31-char limit.

Per-location item detail: each per-location sheet is built from
``report.all_items`` — Task 1's unbounded per-location list covering EVERY
bucket (0-30 through 180+), matching spec §A3's "every SKU" requirement and
the binding mock's per-location row counts (a prior version of this module
built these sheets from ``report.aged_items`` alone, silently dropping every
0-90 day SKU — a blocker finding, fixed by adding ``AgingReport.all_items``).
The "Aged 90+" sheet stays the smaller, aged-only (91-180 / 180+) union across
locations, built from ``report.aged_items`` as before — a distinct, narrower
sheet, never the same rows as a per-location sheet.

``TopItem`` (Task 1) does not carry ``last_restock_date``/``snapshot_date``
fields directly, but both are derivable without approximation: every aged item's
``snapshot_date`` is the report's own ``snapshot_date`` (compute() resolves ONE
snapshot date for the whole report — see ``_resolve_snapshot_date``), and
``last_restock_date = snapshot_date - timedelta(days=item.days)`` by the same
"age = days since last restock" definition ``compute()`` itself uses.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.services.reconciliation.evidence_service import SheetSpec, build_workbook
from app.services.report.inventory_aging import BUCKETS, RESULT_IDS, AgingReport

SUMMARY_SHEET_NAME = "Summary"
BUCKETS_SHEET_NAME = "Buckets"
AGED_SHEET_NAME = "Aged 90+"
METHOD_SHEET_NAME = "Method"

_ALL_LOCATIONS_LABEL = "All locations"

# Description text for the Method sheet — mirrors inventory_aging.py's own
# ``build_sources`` docstring table (spec §A1's source table), not a live
# re-derivation, since a computed ``AgingReport`` carries no query text.
_SOURCE_DESCRIPTIONS: dict[str, str] = {
    "r_items": "Every on-hand SKU per location on the latest snapshot.",
    "r_prior": "The same aggregate per location on the snapshot `compare_days` earlier.",
    "r_trend": "Per location, `trend_weeks` weekly points ending at the latest snapshot.",
    "r_meta": "Per location: first/last snapshot date, snapshot count.",
}


def _kpi_rows(kpis: list[dict]) -> list[list[Any]]:
    return [
        [
            "KPI",
            kpi["label"],
            Decimal(kpi["value"]),
            Decimal(kpi["delta"]),
            Decimal(kpi["delta_pct"]) if kpi["delta_pct"] is not None else "",
            "Yes" if kpi["favourable"] else "No",
            kpi["sub_detail"],
        ]
        for kpi in kpis
    ]


def _location_summary_row(row_type: str, loc: dict) -> list[Any]:
    delta_value = Decimal(loc["delta_value"])
    detail = f"{loc['skus']:,} SKUs · aged {loc['aged90_value']} ({loc['aged90_share_pct']}% of value)"
    return [
        row_type,
        loc["location"],
        Decimal(loc["on_hand_value"]),
        delta_value,
        Decimal(loc["delta_pct"]),
        "Yes" if delta_value >= 0 else "No",
        detail,
    ]


def _summary_sheet(report: dict) -> SheetSpec:
    rows = list(_kpi_rows(report["kpis"]))
    for loc in report["locations"]:
        rows.append(_location_summary_row("Location", loc))
    rows.append(_location_summary_row("Location", report["all_locations"]))
    return {
        "name": SUMMARY_SHEET_NAME,
        "headers": ["Row type", "Label", "Value", "Δ", "Δ %", "Favourable", "Detail"],
        "rows": rows,
    }


def _buckets_sheet(report: dict) -> SheetSpec:
    rows: list[list[Any]] = []
    for loc in (*report["locations"], report["all_locations"]):
        by_bucket = {b["bucket"]: b for b in loc["buckets"]}
        for bucket in BUCKETS:
            b = by_bucket[bucket]
            rows.append(
                [
                    loc["location"],
                    b["bucket"],
                    Decimal(b["value"]),
                    Decimal(b["pct_of_location"]),
                    b["units"],
                    b["skus"],
                ]
            )
    return {
        "name": BUCKETS_SHEET_NAME,
        "headers": ["Location", "Bucket", "Value", "% of location", "Units", "SKUs"],
        "rows": rows,
    }


_LOCATION_SHEET_HEADERS = [
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


def _item_row(item: dict, snapshot_date: date) -> list[Any]:
    last_restock = snapshot_date - timedelta(days=item["days"])
    return [
        item["sku"],
        item["item_desc"],
        item["category"],
        item["units"],
        Decimal(item["value"]),
        item["days"],
        item["bucket"],
        last_restock,
        snapshot_date,
    ]


def _location_sheet(location: str, report: dict, snapshot_date: date) -> SheetSpec:
    rows = [_item_row(item, snapshot_date) for item in report["all_items"].get(location, ())]
    return {"name": location, "headers": _LOCATION_SHEET_HEADERS, "rows": rows}


def _aged_sheet(report: dict, snapshot_date: date) -> SheetSpec:
    rows: list[list[Any]] = []
    for loc in report["locations"]:
        for item in report["aged_items"].get(loc["location"], ()):
            rows.append([loc["location"], *_item_row(item, snapshot_date)])
    return {
        "name": AGED_SHEET_NAME,
        "headers": ["Location", *_LOCATION_SHEET_HEADERS],
        "rows": rows,
    }


def _method_sheet(report: dict) -> SheetSpec:
    prov = report["provenance"]
    rows: list[list[Any]] = []
    for result_id in RESULT_IDS:
        rows.append([result_id, _SOURCE_DESCRIPTIONS.get(result_id, "")])
    rows.append(["source", prov["source"]])
    rows.append(["age_definition", prov["age_definition"]])
    rows.append(["query_count", prov["query_count"]])
    for check in prov["integrity_checks"]:
        rows.append(["integrity_check", check])
    return {
        "name": METHOD_SHEET_NAME,
        "headers": ["Source ID", "Description"],
        "rows": rows,
    }


def build_inventory_aging_workbook(report: AgingReport | dict) -> io.BytesIO:
    """Build the seven-sheet inventory-aging workbook (spec §A3), in order:
    Summary, Buckets, one sheet per location, Aged 90+, Method.

    Gate fix #6/#4: ``report`` may be either the live ``AgingReport`` straight off
    ``inventory_aging.compute()`` OR the JSON-safe dict form a report's persisted
    ``spec_json`` already carries (see ``report_html.build_inventory_aging_sections``'s
    identical boundary-conversion pattern) — ``inventory_aging.json_safe`` is the
    SINGLE conversion point, called exactly once here, idempotent on an
    already-safe dict. Every sheet builder below reads dict form only,
    reconstructing a real ``Decimal`` wherever a cell needs a genuinely numeric
    (not string) value so Excel still sums/formats it as a number."""
    from app.services.report.inventory_aging import json_safe as ia_json_safe

    report_dict = ia_json_safe(report)
    snapshot_date = date.fromisoformat(report_dict["snapshot_date"])
    sheets: list[SheetSpec] = [_summary_sheet(report_dict), _buckets_sheet(report_dict)]
    for loc in report_dict["locations"]:
        sheets.append(_location_sheet(loc["location"], report_dict, snapshot_date))
    sheets.append(_aged_sheet(report_dict, snapshot_date))
    sheets.append(_method_sheet(report_dict))
    return build_workbook(sheets)
