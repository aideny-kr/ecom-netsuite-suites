"""Smart resolver, slice 1: a credit memo the agent proposes, accepted only by outcome.

The agent chooses the items and amounts. The server accepts them only when the order's posted
balance (invoices less credits, tax classified by GL account) then equals the finalized source in
gross, net and tax to the cent. No per-label or per-cause code: R231821517 is just one shape.
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops import credit_creation as cc


def _invoice(total="13494.75", tax="0", remaining=None, location="30"):
    rows = [{"account": "119", "accountingbook": "1", "debit": total}]
    if tax != "0":
        rows.append({"account": "846", "accountingbook": "1", "credit": tax})
    net = str(float(total) - float(tax))
    rows.append({"account": "54", "accountingbook": "1", "credit": f"{float(net):.2f}"})
    doc = {
        "id": "16029044",
        "tranId": "INV371382",
        "total": total,
        "amountRemaining": remaining or total,
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "currency_code": "USD",
        "exchangeRate": "1.0",
        "location": {"id": location} if location else None,
        "department": {"id": "18"},
    }
    return doc, {"complete": True, "rows": rows}


def _facts(**over):
    invoice = _invoice(**{k: over.pop(k) for k in ("total", "tax", "remaining", "location") if k in over})
    facts = {
        "invoices": [invoice],
        "credits": [],
        "source": {
            "number": "R231821517",
            "currency": "USD",
            "state": "complete",
            "completed_at": "2026-09-23T14:11:04Z",
            "total": "12820.02",
            "tax_total": "0",
            "adjustments": [{"label": "reseller discount", "amount": "-674.73", "finalized": True}],
        },
        "profile": {"subsidiary_id": "1", "tax_accounts": ["210", "846"], "tax_item_accounts": {"5005": "210"}},
        "items": {
            "1471": {"id": "1471", "isInactive": False, "itemType": "Discount", "account": {"id": "774"}},
            "5005": {"id": "5005", "isInactive": False, "itemType": "OthCharge", "incomeAccount": {"id": "210"}},
            "900": {"id": "900", "isInactive": False, "itemType": "InvtPart", "incomeAccount": {"id": "54"}},
        },
        "account_types": {"119": "AcctRec", "54": "Income", "774": "Income", "846": "OthCurrLiab"},
        "period": {"id": "173", "closed": False, "arLocked": False, "allLocked": False},
        "posting_date": "2026-10-07",
        "subsidiary_id": "1",
        "precision": 2,
    }
    facts.update(over)
    return facts


LINES = [{"item_id": "1471", "amount": "674.73"}]


def test_r231821517_the_agents_credit_is_accepted_because_the_order_then_equals_solidus():
    result = cc.assess(lines=LINES, memo="reseller discount", **_facts())
    assert result["balance"]["before"] == {"gross": "13494.75", "net": "13494.75", "tax": "0.00"}
    assert (
        result["balance"]["after"]
        == result["balance"]["source"]
        == {"gross": "12820.02", "net": "12820.02", "tax": "0.00"}
    )
    f = result["proposed_fields"]
    assert f["entity"] == {"id": "5658593"} and f["subsidiary"] == {"id": "1"} and f["currency"] == {"id": "1"}
    assert f["account"] == {"id": "119"} and f["postingPeriod"] == {"id": "173"} and f["tranDate"] == "2026-10-07"
    assert f["memo"] == "R231821517 reseller discount"
    assert f["item"]["items"] == [
        {"item": {"id": "1471"}, "quantity": 1, "rate": "674.73", "amount": "674.73", "isTaxable": False}
    ]
    assert f["apply"]["items"] == [{"doc": {"id": "16029044"}, "apply": True, "amount": "674.73"}]
    assert f["autoApply"] is False and f["location"] == {"id": "30"}
    assert result["expected_ledger"] == {"debit": {"774": "674.73"}, "credit": {"119": "674.73"}}
    assert result["expected_after"] == {"total": "674.73", "subtotal": "674.73", "taxTotal": "0.00"}


@pytest.mark.parametrize(
    "lines, code",
    [
        ([{"item_id": "1471", "amount": "600.00"}], "outcome_does_not_match_source"),
        (
            [{"item_id": "1471", "amount": "674.73"}, {"item_id": "1471", "amount": "0.01"}],
            "outcome_does_not_match_source",
        ),
        ([{"item_id": "900", "amount": "674.73"}], "item_not_allowed"),
        ([{"item_id": "777", "amount": "674.73"}], "item_not_allowed"),
        ([{"item_id": "1471", "amount": "674.7"}], None),
        ([{"item_id": "1471", "amount": "-674.73"}], "invalid_amount"),
        ([{"item_id": "1471", "amount": 674.73}], "invalid_amount"),
        ([], "invalid_amount"),
    ],
)
def test_a_proposal_that_does_not_reconcile_or_uses_a_wrong_item_is_refused_with_the_numbers(lines, code):
    if code is None:  # "674.7" is a valid amount but does not reconcile
        code = "outcome_does_not_match_source"
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=lines, memo="reseller discount", **_facts())
    assert exc.value.code == code
    if code == "outcome_does_not_match_source":
        assert exc.value.detail["required"]["gross"] == "12820.02"


def test_no_difference_means_no_credit():
    facts = _facts(total="12820.02")
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **facts)
    assert exc.value.code == "no_difference"


def test_netsuite_below_solidus_cannot_be_fixed_by_a_credit():
    facts = _facts(total="12000.00")
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **facts)
    assert exc.value.code == "netsuite_below_source"


def test_existing_credits_count_before_the_new_one():
    facts = _facts()
    old = {
        "id": "15",
        "total": "674.73",
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "exchangeRate": "1.0",
    }
    facts["credits"] = [
        (
            old,
            {
                "complete": True,
                "rows": [
                    {"account": "119", "accountingbook": "1", "credit": "674.73"},
                    {"account": "774", "accountingbook": "1", "debit": "674.73"},
                ],
            },
        )
    ]
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **facts)
    assert exc.value.code == "no_difference"  # the order already equals Solidus: never a second credit


def test_a_vat_refund_reverses_tax_only_through_the_configured_tax_item():
    # NetSuite charged 20.00 tax that Solidus did not; the credit reverses exactly that tax.
    facts = _facts(total="120.00", tax="20.00")
    facts["source"].update(total="100.00", tax_total="0")
    facts["profile"]["tax_item_accounts"] = {"5005": "846"}
    facts["items"]["5005"]["incomeAccount"] = {"id": "846"}
    result = cc.assess(lines=[{"item_id": "5005", "amount": "20.00"}], memo="VAT refund", **facts)
    assert result["balance"]["after"] == {"gross": "100.00", "net": "100.00", "tax": "0.00"}
    assert result["expected_ledger"]["debit"] == {"846": "20.00"}
    assert result["expected_after"]["taxTotal"] == "20.00"


def test_tax_reversal_cannot_exceed_the_tax_the_invoice_posted():
    facts = _facts(total="120.00", tax="20.00")
    facts["source"].update(total="95.00", tax_total="0")
    facts["profile"]["tax_item_accounts"] = {"5005": "846"}
    facts["items"]["5005"]["incomeAccount"] = {"id": "846"}
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=[{"item_id": "5005", "amount": "25.00"}], memo="x", **facts)
    assert exc.value.code == "tax_reversal_exceeds_posted"


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda f: f["period"].update(closed=True), "period_locked"),
        (lambda f: f["period"].update(arLocked=True), "period_locked"),
        (lambda f: f["source"].update(state="cart"), "source_not_final"),
        (lambda f: f["source"]["adjustments"][0].update(finalized=False), "source_not_final"),
        (
            lambda f: (
                f["invoices"][0][0].update(entity={"id": "999"})
                or f["credits"].append(
                    (
                        {**deepcopy(f["invoices"][0][0]), "id": "77", "entity": {"id": "1"}},
                        {"complete": True, "rows": []},
                    )
                )
            ),
            "credit_scope_mismatch",
        ),
        (lambda f: f["invoices"][0][0].update(exchangeRate="1.1"), "foreign_currency_unsupported"),
        (lambda f: f["invoices"][0][0].update(amountRemaining="100.00"), "invoice_remaining_too_small"),
        (lambda f: f["invoices"].append(deepcopy(f["invoices"][0])), "invoice_count_unsupported"),
    ],
)
def test_scope_period_source_and_invoice_guards_refuse_specifically(mutate, code):
    facts = _facts()
    mutate(facts)
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **facts)
    assert exc.value.code == code


def test_the_memo_always_names_the_order():
    result = cc.assess(lines=LINES, memo="R231821517 reseller discount", **_facts())
    assert result["proposed_fields"]["memo"] == "R231821517 reseller discount"
    assert cc.assess(lines=LINES, memo="", **_facts())["proposed_fields"]["memo"] == "R231821517"


def test_a_missing_invoice_location_uses_the_configured_correction_location_or_refuses():
    facts = _facts(location=None)
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **facts)
    assert exc.value.code == "credit_location_required"
    facts["profile"]["correction_location_id"] = "81"
    assert cc.assess(lines=LINES, memo="x", **facts)["proposed_fields"]["location"] == {"id": "81"}


def test_readback_balance_reports_the_order_as_it_stands():
    facts = _facts()
    booked, required = cc.booked_balance(**facts)
    assert booked["gross"] == "13494.75" and required["gross"] == "12820.02"


def test_an_unconfigured_item_that_posts_to_a_tax_account_is_refused():
    """Tax is reversed only through the subsidiary's configured tax-refund items."""
    facts = _facts(total="120.00", tax="20.00")
    facts["source"].update(total="100.00", tax_total="0")
    facts["items"]["6000"] = {
        "id": "6000",
        "isInactive": False,
        "itemType": "OthCharge",
        "incomeAccount": {"id": "846"},
    }
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=[{"item_id": "6000", "amount": "20.00"}], memo="x", **facts)
    assert (exc.value.code, exc.value.detail.get("reason")) == ("item_not_allowed", "unconfigured_item_posts_to_tax")
