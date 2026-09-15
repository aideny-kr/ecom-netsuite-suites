from datetime import datetime, timezone

import pytest

from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.runner import build_report


def evidence():
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    source = {
        "source": "framework",
        "scope": "order",
        "page_complete": True,
        "read_at": now.isoformat(),
        "orders": [
            {
                "id": "100",
                "number": "R100000001",
                "currency": "USD",
                "business_entity": "Framework Inc",
                "total": "120.00",
                "tax_total": "20.00",
                "included_tax_total": "0",
                "additional_tax_total": "20.00",
                "updated_at": now.isoformat(),
            }
        ],
    }
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "6738075", "subsidiary_id": "1"},
        "observed_at": now.isoformat(),
        "lookup": {"count": 1, "complete": True},
        "orders": [
            {
                "record_id": "200",
                "order_reference": "R100000001",
                "header_complete": True,
                "complete": False,
                "header": {
                    "id": "200",
                    "total": "119.00",
                    "taxTotal": "19.00",
                    "currency": {"id": "1"},
                    "subsidiary": {"id": "1"},
                    "lastModifiedDate": now.isoformat(),
                },
                "currency_metadata": {"id": "1", "symbol": "USD", "currencyPrecision": 2},
                "completeness_errors": ["lines_not_expanded"],
            }
        ],
    }
    mapping = TransactionMapping(
        reference_field="tranid", currency_minor_units={"USD": 2}, business_entity_subsidiaries={"Framework Inc": "1"}
    )
    config = {
        "netsuite_account_id": "6738075",
        "subsidiary_id": "1",
        "record_type": "salesorder",
        "mapping_json": mapping.model_dump(),
    }
    return source, target, config, mapping, now


def test_known_order_and_tax_differences_remain_visible_without_repair_eligibility():
    source, target, config, mapping, now = evidence()
    report = build_report(source, target, config, mapping, now=now)
    assert report["balance"]["status"] == "difference"
    assert report["balance"]["amounts"]["order_total"]["delta"] == "1.00"
    assert report["balance"]["amounts"]["tax"]["delta"] == "1.00"
    assert report["balance"]["amounts"]["refunds"]["source"] is None
    assert report["comparison"]["recommended_action"] in {"gather_evidence", "human_review"}
    assert report["source"]["lines_complete"] is False


@pytest.mark.parametrize("metric", ["order_total", "tax", "refunds"])
@pytest.mark.parametrize("direction", [-1, 1])
def test_one_cent_is_a_real_difference_in_each_financial_dimension(metric, direction):
    from decimal import Decimal

    source, target, config, mapping, now = evidence()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00")
    refunds = {
        side: {"order_reference": "R100000001", "currency": "USD", "amount": "1.00", "complete": True}
        for side in ("source", "target")
    }
    delta = Decimal(direction) / 100
    if metric == "refunds":
        refunds["target"]["amount"] = str(Decimal("1.00") - delta)
    else:
        field = "total" if metric == "order_total" else "taxTotal"
        header = target["orders"][0]["header"]
        header[field] = str(Decimal(header[field]) - delta)
    result = build_report(source, target, config, mapping, now=now, refunds=refunds)["balance"]
    assert result["status"] == "difference"
    assert result["amounts"][metric]["delta"] == f"{delta:.2f}"
    assert result["missing_metrics"] == []


def test_matching_headers_do_not_turn_unknown_refunds_into_a_match():
    source, target, config, mapping, now = evidence()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00")
    report = build_report(source, target, config, mapping, now=now)
    assert report["balance"]["status"] == "incomplete"
    assert report["balance"]["missing_metrics"] == ["refunds"]


def test_balance_fallback_cannot_bypass_account_scope_validation():
    source, target, config, mapping, now = evidence()
    target["scope"]["account_id"] = "9999999"
    with pytest.raises(ValueError, match="target_scope_mismatch"):
        build_report(source, target, config, mapping, now=now)


def test_header_fallback_preserves_the_existing_legacy_identity_guard():
    source, target, config, mapping, now = evidence()
    source["orders"][0]["business_entity"] = "legacy"
    mapping = mapping.model_copy(update={"business_entity_subsidiaries": {"legacy": "1"}})
    config["mapping_json"] = mapping.model_dump()
    report = build_report(source, target, config, mapping, now=now)
    assert report["source"]["subsidiary_id"] is None
    assert report["balance"]["status"] == "incomplete"
