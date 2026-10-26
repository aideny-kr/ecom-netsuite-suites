"""Order reconciliation must prove all totals without confusing unknown and zero."""

from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order


def evidence():
    source = {
        "source": "framework",
        "scope": "order",
        "page_complete": True,
        "read_at": "2026-09-06T06:00:00Z",
        "orders": [
            {
                "number": "R123456789",
                "currency": "GBP",
                "business_entity": "UK",
                "total": "120.00",
                "included_tax_total": "20.00",
                "additional_tax_total": "0.00",
            }
        ],
    }
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "123-sb1", "subsidiary_id": "5"},
        "observed_at": "2026-09-06T06:00:01Z",
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "order_reference": "R123456789",
                "header_complete": True,
                "header": {
                    "id": "77",
                    "subsidiary": {"id": "5"},
                    "currency": {"id": "2"},
                    "total": "120.00",
                    "taxTotal": "20.00",
                },
                "currency_metadata": {"id": "2", "symbol": "GBP", "currencyPrecision": 2},
            }
        ],
    }
    config = {
        "netsuite_account_id": "123_SB1",
        "subsidiary_id": "5",
        "mapping_json": {"business_entity_subsidiaries": {"UK": "5"}},
    }
    refund = {"complete": True, "order_reference": "R123456789", "currency": "GBP", "amount": "0.00"}
    return source, target, config, {"source": deepcopy(refund), "target": deepcopy(refund)}


def test_order_total_tax_and_refunds_all_agree():
    source, target, config, refunds = evidence()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert set(result["amounts"]) == {"order_total", "tax", "refunds"}
    assert all(row["delta"] == "0.00" for row in result["amounts"].values())


def test_negative_source_order_remains_unverified_instead_of_becoming_a_match():
    source, target, config, refunds = evidence()
    source["orders"][0]["total"] = "-16.00"
    target["orders"][0]["header"]["total"] = "-16.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "incomplete"
    assert result["missing_metrics"]


def test_unverified_target_provider_cannot_prove_a_match():
    source, target, config, refunds = evidence()
    target["provider"] = "model_guess"
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "incomplete"


@pytest.mark.parametrize(
    "entity,expected", [(None, "matched"), ("legacy", "incomplete"), ({"id": "legacy"}, "incomplete")]
)
def test_only_explicit_null_uses_a_configured_legacy_entity(entity, expected):
    source, target, config, refunds = evidence()
    source["orders"][0]["business_entity"] = entity
    config["mapping_json"]["business_entity_subsidiaries"] = {"legacy": "5"}
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == expected


def test_partial_refunds_are_compared_separately_from_gross_order_total():
    source, target, config, refunds = evidence()
    refunds["source"]["amount"] = "60.00"
    refunds["target"]["amount"] = "40.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["refunds"]["delta"] == "20.00"
    assert result["amounts"]["order_total"]["delta"] == "0.00"


def test_vat_is_not_added_to_the_already_tax_inclusive_order_total():
    source, target, config, refunds = evidence()
    target["orders"][0]["header"].update(total="119.99", taxTotal="19.99")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["amounts"]["tax"]["delta"] == "0.01"
    assert result["amounts"]["order_total"]["delta"] == "0.01"


@pytest.mark.parametrize("missing", [None, {"source": {"complete": False}, "target": {"complete": False}}])
def test_unknown_refunds_cannot_become_zero_or_a_match(missing):
    source, target, config, _ = evidence()
    result = reconcile_order(source, target, config, refunds=missing)
    assert result["status"] == "incomplete"
    assert result["amounts"]["refunds"] == {"source": None, "target": None, "delta": None}


@pytest.mark.parametrize("kind", ["currency", "subsidiary", "reference", "account", "incomplete_lookup", "duplicate"])
def test_identity_and_coverage_fail_closed(kind):
    source, target, config, refunds = evidence()
    expected = "incomplete"
    if kind == "currency":
        target["orders"][0]["currency_metadata"]["symbol"] = "EUR"
        expected = "currency_mismatch"
    elif kind == "subsidiary":
        target["orders"][0]["header"]["subsidiary"]["id"] = "6"
    elif kind == "reference":
        target["orders"][0]["order_reference"] = "R000000000"
    elif kind == "account":
        target["scope"]["account_id"] = "999"
    elif kind == "incomplete_lookup":
        target["lookup"]["complete"] = False
    else:
        target["orders"].append(deepcopy(target["orders"][0]))
        target["lookup"]["count"] = 2
        expected = "ambiguous"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == expected
    assert all(v["delta"] is None for v in result["amounts"].values())


def test_complete_empty_lookup_proves_missing_order():
    source, target, config, refunds = evidence()
    target.update(orders=[], lookup={"complete": True, "count": 0})
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "missing_in_netsuite"


@pytest.mark.parametrize("value", [None, True, 120.0, "NaN", "120.001"])
def test_unknown_or_invalid_money_cannot_match(value):
    source, target, config, refunds = evidence()
    source["orders"][0]["total"] = value
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "incomplete"
    assert result["amounts"]["order_total"]["delta"] is None


def test_refund_from_another_order_or_currency_cannot_cover_this_order():
    source, target, config, refunds = evidence()
    refunds["source"].update(currency="EUR", order_reference="R000000000")
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "incomplete"


# --- Credit memos created from the order's invoice (decided 2026-09-30/10-01) ---------
# Framework books a Solidus order adjustment as a credit memo created from the invoice
# and leaves the sales order unchanged. When those credits explain the difference
# exactly, total and tax together, the order is matched and the credits are its reason.


def credited(evidence_tuple, source_total, credits, *, source_tax="20.00", invoiced=("120.00", "20.00")):
    source, target, config, refunds = evidence_tuple
    source["orders"][0].update(total=source_total, included_tax_total=source_tax)
    refunds["target"]["dependency_manifest"] = {"version": 1, "order_id": "77", "transaction_ids": ["77"]}
    refunds["target"]["invoice_credits"] = {
        "complete": True,
        "credits": [
            {"id": str(30 + i), "number": f"CM{i}", "invoice_id": "9", "total": total, "tax": tax, "posting": "income"}
            for i, (total, tax) in enumerate(credits)
        ],
        "total": str(sum((Decimal(t) for t, _ in credits), Decimal(0))),
        "tax": str(sum((Decimal(x) for _, x in credits), Decimal(0))),
        # The order's invoices as billed; they must equal the sales order before a credit counts.
        "invoices": {"ids": ["9"], "total": invoiced[0], "tax": invoiced[1]},
    }
    return source, target, config, refunds


def test_invoice_credits_that_exactly_explain_the_difference_reconcile_the_order():
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "verified_adjustments_agree"
    assert result["amounts"]["order_total"] == {"source": "115.18", "target": "115.18", "delta": "0.00"}
    assert result["original_amounts"]["order_total"]["delta"] == "-4.82"
    credit = result["adjustments"][-1]
    assert credit["kind"] == "invoice_credit_memos"
    assert [c["number"] for c in credit["credits"]] == ["CM0"]
    assert credit["total"] == "4.82" and credit["tax"] == "0.00"


def test_several_invoice_credits_count_together():
    source, target, config, refunds = credited(evidence(), "110.00", [("4.00", "0"), ("6.00", "0")])
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "matched"


def test_credits_that_do_not_explain_the_difference_exactly_leave_it_visible():
    source, target, config, refunds = credited(evidence(), "115.00", [("4.82", "0")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["order_total"] == {"source": "115.00", "target": "120.00", "delta": "-5.00"}
    assert "adjustments" not in result


def test_an_order_that_already_matches_ignores_its_return_credits():
    source, target, config, refunds = credited(evidence(), "120.00", [("30.00", "5.00")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "all_amounts_agree"
    assert result["amounts"]["order_total"]["target"] == "120.00"


def test_a_taxed_credit_must_explain_the_tax_too():
    # The credit carries 1.00 tax, but Solidus kept its tax at 20.00: the total and the
    # tax cannot both agree, so nothing is explained.
    source, target, config, refunds = credited(evidence(), "114.18", [("5.82", "1.00")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["order_total"]["target"] == "120.00"
    assert result["amounts"]["tax"]["target"] == "20.00"


def test_a_taxed_credit_matching_the_lower_source_tax_reconciles_both():
    source, target, config, refunds = credited(evidence(), "114.18", [("5.82", "1.00")], source_tax="19.00")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert result["amounts"]["tax"] == {"source": "19.00", "target": "19.00", "delta": "0.00"}


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["target"]["invoice_credits"].update(complete=False),
        lambda r: r["target"].pop("invoice_credits"),
        lambda r: r["target"]["dependency_manifest"].update(order_id="78"),
        lambda r: r["target"].pop("dependency_manifest"),
        lambda r: r["target"].update(order_reference="R999999999"),
        lambda r: r["target"].update(currency="USD"),
        lambda r: r["target"].update(complete=False),
        lambda r: r["target"]["invoice_credits"].update(total="4.8"),  # the sum must match its credits
        lambda r: r["target"]["invoice_credits"]["credits"][0].update(total="-4.82"),
        lambda r: r["target"]["invoice_credits"]["credits"].clear(),
        lambda r: r["target"]["invoice_credits"].pop("invoices"),
        lambda r: r["target"]["invoice_credits"]["invoices"].update(ids=[]),
        lambda r: r["target"]["invoice_credits"]["invoices"].update(total="abc"),
        lambda r: r["target"]["invoice_credits"]["invoices"].pop("tax"),
        lambda r: r["target"]["invoice_credits"]["credits"][0].update(posting="unproven"),
        lambda r: r["target"]["invoice_credits"]["credits"][0].pop("posting"),
    ],
)
def test_unproven_invoice_credits_never_explain_a_difference(change):
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")])
    change(refunds)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["amounts"]["order_total"]["target"] == "120.00"
    assert result["status"] != "matched"


def test_unknown_source_tax_never_lets_credits_erase_a_known_total_difference():
    # Packet review F2: the credits must explain total and tax together.
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")])
    del source["orders"][0]["included_tax_total"]
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["amounts"]["order_total"] == {"source": "115.18", "target": "120.00", "delta": "-4.82"}
    assert "adjustments" not in result


def test_credits_on_an_invoice_that_differs_from_the_sales_order_never_explain_it():
    # Packet review 2026-10-16, F1: the sales order says 120.00 but its invoice was already
    # billed at 115.18. A further 4.82 credit makes sales order less credit equal the source,
    # yet the customer was billed 110.36. The credits explain a difference only when the
    # invoices equal the sales order they are subtracted from.
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")], invoiced=("115.18", "20.00"))
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["order_total"] == {"source": "115.18", "target": "120.00", "delta": "-4.82"}
    assert "adjustments" not in result


def test_credits_on_an_invoice_whose_tax_differs_from_the_sales_order_never_explain_it():
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")], invoiced=("120.00", "19.00"))
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "difference"


def test_a_refund_difference_left_by_an_open_return_authorization_says_so():
    """C09 (2026-10-25): the NetSuite read leaves out a hand-booked refund credit whose return
    authorization is still open, so the order is not matched while the authorization could pay the
    customer again. The difference names the open authorization for the reviewer to close."""
    source, target, config, refunds = evidence()
    refunds["source"]["amount"] = "200.00"
    refunds["target"].update(
        tagged_credits=[],
        tagged_credits_rejected=[
            {"id": "1003", "reason": "return_authorization_open", "return_authorization_id": "1002"},
            {"id": "1008", "reason": "customer_differs", "return_authorization_id": "1009"},
            {"id": "1010", "reason": "return_authorization_open", "return_authorization_id": "RA1"},
        ],
    )
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["reason"] == "return_authorization_open_after_manual_refund"
    assert result["open_return_authorizations"] == ["1002"]
    assert result["amounts"]["refunds"] == {"source": "200.00", "target": "0.00", "delta": "200.00"}
    refunds["target"]["tagged_credits_rejected"] = refunds["target"]["tagged_credits_rejected"][1:2]
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["reason"] == "amounts_differ" and "open_return_authorizations" not in result


HELD = {"id": "1003", "reason": "return_authorization_open", "return_authorization_id": "1002"}


@pytest.mark.parametrize("change", ["refunds_agree", "order_total_differs", "unreadable_authorization"])
def test_an_open_return_authorization_never_lets_the_order_match(change):
    """C09-R1 (2026-10-25): the held refund is NetSuite money the read leaves out. A source refund that
    already equals what was counted (none at all, or one NetSuite paid twice) would match the order and
    hide it, so the hold alone keeps the order a difference, whatever the amounts and whichever
    metric differs. An authorization whose id cannot be read still holds the order."""
    source, target, config, refunds = evidence()
    held = dict(HELD)
    if change == "order_total_differs":
        target["orders"][0]["header"]["total"] = "119.00"
    if change == "unreadable_authorization":
        held["return_authorization_id"] = "RA1"
    refunds["target"].update(tagged_credits=[], tagged_credits_rejected=[held])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert (result["status"], result["reason"]) == ("difference", "return_authorization_open_after_manual_refund")
    assert result["open_return_authorizations"] == ([] if change == "unreadable_authorization" else ["1002"])
    assert result["amounts"]["refunds"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}


def test_an_incomplete_comparison_still_names_the_open_return_authorization():
    """Named on an incomplete result too, so a later step that re-derives the missing amounts (a posting
    recheck) cannot match the order while the authorization is open. Only the order's own complete
    read names one."""
    source, target, config, refunds = evidence()
    del source["orders"][0]["included_tax_total"]
    refunds["target"].update(tagged_credits=[], tagged_credits_rejected=[dict(HELD)])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert (result["status"], result["reason"]) == ("incomplete", "amount_evidence_unavailable")
    assert result["open_return_authorizations"] == ["1002"]
    refunds["target"]["order_reference"] = "R123456780"
    assert "open_return_authorizations" not in reconcile_order(source, target, config, refunds=refunds)
