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


# --- Credit memos created from the order's own return authorization (C15) ----------------
# A returned order is credited from its return authorization, so the invoice credits never
# include that credit. Two tiers, never added together by default: the invoice credits are
# tried first, alone, exactly as before. Only when they do not explain the difference are
# they tried together with every one of the order's own return credits, all income, exact on
# total and tax, against invoices equal to the sales order. Neither tier is ever applied in part.


def returned(source, sales_order, credits=(), returns=(), *, refunded="0.00"):
    """Synthetic order R900000801: the source (total, tax), the sales order and its invoice (total,
    tax), the invoice credits and the order's own return credits, each (total, tax)."""
    evidence_tuple = evidence()
    source_evidence, target, config, refunds = evidence_tuple
    reference = "R900000801"
    source_evidence["orders"][0].update(number=reference, total=source[0], included_tax_total=source[1])
    target["orders"][0]["order_reference"] = reference
    target["orders"][0]["header"].update(total=sales_order[0], taxTotal=sales_order[1])
    for side in refunds.values():
        side.update(order_reference=reference, amount=refunded)
    refunds["target"]["dependency_manifest"] = {"version": 1, "order_id": "77", "transaction_ids": ["77"]}

    def read(values, origin, first):
        rows = [
            {
                "id": str(first + i),
                "number": f"CM{first + i}",
                "origin_id": origin[0],
                "origin_type": origin[1],
                **({"invoice_id": origin[0]} if origin[1] == "CustInvc" else {}),
                "total": total,
                "tax": tax,
                "posting": "income",
            }
            for i, (total, tax) in enumerate(values)
        ]
        return {
            "credits": rows,
            "total": str(sum((Decimal(t) for t, _ in values), Decimal(0))),
            "tax": str(sum((Decimal(x) for _, x in values), Decimal(0))),
        }

    refunds["target"]["invoice_credits"] = {
        "complete": True,
        **read(credits, ("9", "CustInvc"), 30),
        "invoices": {"ids": ["9"], "total": sales_order[0], "tax": sales_order[1]},
        "return_credits": {"complete": True, **read(returns, ("40", "RtnAuth"), 60)},
    }
    return evidence_tuple


def test_a_return_credit_applied_to_the_invoice_reconciles_a_fully_returned_order():
    # Returned in full: the credit created from the return authorization is applied to the invoice.
    source, target, config, refunds = returned(("0.00", "0.00"), ("500.00", "0.00"), returns=[("500.00", "0.00")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "verified_adjustments_agree"
    assert result["amounts"]["order_total"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}
    credit = result["adjustments"][-1]
    assert credit["kind"] == "invoice_credit_memos" and credit["status"] == "verified"
    assert credit["basis"] == "credit memos created from the order's invoice or its own return authorization"
    assert credit["credits"] == [
        {"id": "60", "number": "CM60", "origin_id": "40", "origin_type": "RtnAuth", "total": "500.00", "tax": "0.00"}
    ]
    assert credit["total"] == "500.00" and credit["tax"] == "0.00"


def test_a_return_credit_short_on_tax_leaves_the_whole_difference_and_the_refund_unchanged():
    # Returned in full and the credit refunded, never applied to the invoice: credited 500.00 with
    # 92.50 tax against 100.00 billed. Total agrees after the credit, tax does not, so nothing is
    # applied: the report keeps the full differences and the refunds still agree on their own.
    source, target, config, refunds = returned(
        ("0.00", "0.00"), ("500.00", "100.00"), returns=[("500.00", "92.50")], refunded="500.00"
    )
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["order_total"] == {"source": "0.00", "target": "500.00", "delta": "-500.00"}
    assert result["amounts"]["tax"] == {"source": "0.00", "target": "100.00", "delta": "-100.00"}
    assert result["amounts"]["refunds"] == {"source": "500.00", "target": "500.00", "delta": "0.00"}
    assert "adjustments" not in result


def test_an_order_that_already_matches_ignores_its_return_credits_too():
    source, target, config, refunds = returned(("120.00", "20.00"), ("120.00", "20.00"), returns=[("30.00", "5.00")])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "all_amounts_agree"
    assert "adjustments" not in result


def test_invoice_credits_that_explain_the_difference_alone_still_match_on_them_alone():
    # Regression A: Solidus does not lower its total for a return, so adding return credit 60
    # to invoice credit 30 would break a match that holds today.
    source, target, config, refunds = returned(
        ("115.18", "20.00"), ("120.00", "20.00"), credits=[("4.82", "0.00")], returns=[("30.00", "0.00")]
    )
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    credit = result["adjustments"][-1]
    assert credit["basis"] == "credit memos created from the order's invoice"
    assert [c["id"] for c in credit["credits"]] == ["30"] and credit["total"] == "4.82"


@pytest.mark.parametrize(
    "returns",
    [
        lambda r: r["credits"][0].update(posting="unproven"),  # a restocking fee posted to an expense
        lambda r: r.clear() or r.update(complete=False, reason="return_credit_ownership_ambiguous"),
    ],
)
def test_an_unproven_return_credit_never_breaks_an_invoice_credit_match(returns):
    # Regression B: a problem with a return credit makes only the return credits unknown.
    source, target, config, refunds = returned(
        ("115.18", "20.00"), ("120.00", "20.00"), credits=[("4.82", "0.00")], returns=[("30.00", "0.00")]
    )
    returns(refunds["target"]["invoice_credits"]["return_credits"])
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert [c["id"] for c in result["adjustments"][-1]["credits"]] == ["30"]


def test_invoice_and_return_credits_count_together_only_when_the_invoice_credits_alone_do_not():
    source, target, config, refunds = returned(
        ("85.18", "20.00"), ("120.00", "20.00"), credits=[("4.82", "0.00")], returns=[("30.00", "0.00")]
    )
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    credit = result["adjustments"][-1]
    assert [(c["id"], c["origin_type"]) for c in credit["credits"]] == [("30", "CustInvc"), ("60", "RtnAuth")]
    assert credit["credits"][0]["invoice_id"] == "9" and "invoice_id" not in credit["credits"][1]
    assert credit["total"] == "34.82" and credit["tax"] == "0.00"


def test_return_credits_count_all_together_or_not_at_all():
    # 30.00 alone would explain the 30.00 difference; the order's return credits are never a subset.
    source, target, config, refunds = returned(
        ("90.00", "20.00"), ("120.00", "20.00"), returns=[("30.00", "0.00"), ("10.00", "0.00")]
    )
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference" and "adjustments" not in result


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["target"]["invoice_credits"]["return_credits"].update(complete=False),
        lambda r: r["target"]["invoice_credits"].pop("return_credits"),
        lambda r: r["target"]["invoice_credits"]["return_credits"]["credits"][0].update(posting="unproven"),
        lambda r: r["target"]["invoice_credits"]["return_credits"]["credits"][0].pop("posting"),
        lambda r: r["target"]["invoice_credits"]["return_credits"].update(total="29.00"),
        lambda r: r["target"]["invoice_credits"]["return_credits"]["credits"][0].update(total="-30.00"),
        lambda r: r["target"]["invoice_credits"]["return_credits"]["credits"].clear(),
        lambda r: r["target"]["invoice_credits"]["credits"][0].update(posting="unproven"),
        lambda r: r["target"]["invoice_credits"].update(complete=False),
        lambda r: r["target"]["invoice_credits"]["invoices"].update(total="115.18"),
        lambda r: r["target"].update(tax_adjustments=[{"credit_memo_id": "60"}]),
    ],
)
def test_unproven_return_credits_never_explain_a_difference(change):
    source, target, config, refunds = returned(
        ("85.18", "20.00"), ("120.00", "20.00"), credits=[("4.82", "0.00")], returns=[("30.00", "0.00")]
    )
    change(refunds)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["amounts"]["order_total"]["target"] == "120.00"
    assert result["status"] != "matched"


@pytest.mark.parametrize(
    "case",
    [
        (("115.18", "20.00"), [("4.82", "0.00")]),  # explained by the invoice credit
        (("115.00", "20.00"), [("4.82", "0.00")]),  # not explained
        (("0.00", "0.00"), []),  # a cash-sale order: no invoice credit at all
    ],
)
def test_unknown_return_credits_leave_every_result_exactly_as_before(case):
    (source_amounts, credits) = case
    before = returned(source_amounts, ("120.00", "20.00"), credits=credits)
    before[3]["target"]["invoice_credits"].pop("return_credits")
    after = returned(source_amounts, ("120.00", "20.00"), credits=credits)
    after[3]["target"]["invoice_credits"]["return_credits"] = {
        "complete": False,
        "reason": "invoice_credit_billing_unproven",
    }
    assert reconcile_order(*after[:3], refunds=after[3]) == reconcile_order(*before[:3], refunds=before[3])


def test_a_saved_invoice_credit_without_its_origin_is_copied_as_before():
    source, target, config, refunds = credited(evidence(), "115.18", [("4.82", "0")])
    credit = reconcile_order(source, target, config, refunds=refunds)["adjustments"][-1]
    assert credit["basis"] == "credit memos created from the order's invoice"
    assert credit["credits"] == [{"id": "30", "number": "CM0", "invoice_id": "9", "total": "4.82", "tax": "0"}]
