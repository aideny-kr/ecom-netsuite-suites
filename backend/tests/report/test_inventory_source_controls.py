"""Independent source inconsistencies must fail before rendering or publication."""

from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.report.inventory_aging import SourceIntegrityError, validate_source_controls
from app.services.report.playbooks import rebuild_playbook_spec
from app.services.report.refresh_service import RefreshError
from tests.report.test_inventory_aging import _full_fixture


def test_consistent_source_controls_accept_numeric_strings_and_rounded_percentages():
    data, params = _full_fixture()
    for rows in data.values():
        for row in rows:
            for key, value in row.items():
                if isinstance(value, (int, float, Decimal)):
                    row[key] = str(value)
    validate_source_controls(data, params)


def corrupt(data, case):
    if case == "current_total":
        data["r_items"][0]["inventory_amount"] += 1
    elif case == "aged_total":
        data["r_items"][0].update(days=100, last_restock_date="2026-05-31")
    elif case == "item_count":
        data["r_items"][0]["qty_on_hand"] += 1
    elif case == "prior_total":
        data["r_prior"][0]["value"] += 1
    elif case == "trend_percentage":
        data["r_trend"][0]["pct_90p"] += 1
    elif case == "duplicate_item":
        data["r_items"].append(deepcopy(data["r_items"][0]))
    elif case == "duplicate_trend":
        data["r_trend"].append(deepcopy(data["r_trend"][0]))
    elif case == "duplicate_prior":
        data["r_prior"].append(deepcopy(data["r_prior"][0]))
    elif case == "missing_location":
        data["r_items"] = [row for row in data["r_items"] if row["location"] != "Acme"]
    elif case == "unexpected_location":
        data["r_items"][0]["location"] = "Elsewhere"
    elif case == "mixed_dates":
        data["r_items"][0]["snapshot_date"] = "2026-09-07"
    elif case == "wrong_prior_date":
        data["r_prior"][0]["snapshot_date"] = "2026-09-02"
    elif case == "missing_prior_date":
        del data["r_prior"][0]["snapshot_date"]
    elif case == "stale_metadata":
        data["r_meta"][0]["last_snapshot_date"] = "2026-09-09"
    elif case == "fractional_quantity":
        data["r_items"][0]["qty_on_hand"] = "1.1"
    elif case == "missing_value":
        del data["r_items"][0]["inventory_amount"]
    elif case == "nonfinite":
        data["r_items"][0]["inventory_amount"] = "NaN"
    elif case == "invalid_count":
        data["r_prior"][0]["skus_90p"] = 1000
    elif case == "missing_source":
        del data["r_meta"]
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "current_total",
        "item_count",
        "aged_total",
        "prior_total",
        "trend_percentage",
        "duplicate_item",
        "duplicate_trend",
        "duplicate_prior",
        "missing_location",
        "unexpected_location",
        "mixed_dates",
        "wrong_prior_date",
        "missing_prior_date",
        "stale_metadata",
        "fractional_quantity",
        "missing_value",
        "nonfinite",
        "invalid_count",
        "missing_source",
    ],
)
def test_inconsistent_sources_fail_closed(case):
    data, params = _full_fixture()
    corrupt(data, case)
    with pytest.raises(SourceIntegrityError, match="Inventory source controls failed"):
        validate_source_controls(data, params)


def test_rebuild_gate_blocks_inconsistent_source_before_render(monkeypatch):
    data, params = _full_fixture()
    corrupt(data, "prior_total")
    tables = {rid: {"columns": list(rows[0]), "rows": [list(r.values()) for r in rows]} for rid, rows in data.items()}

    def forbidden(*args, **kwargs):
        pytest.fail("invalid report reached renderer")

    monkeypatch.setattr("app.services.report.report_html.build_inventory_aging_sections", forbidden)
    with pytest.raises(RefreshError, match="prior and same-date trend totals disagree"):
        rebuild_playbook_spec("inventory_aging", params, tables, composed_at="2026-10-01T00:00:00Z")
