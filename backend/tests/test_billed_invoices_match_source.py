"""C14b (2026-10-27): a fully billed order whose posted invoices equal the source, its sales order stale.

order_reconciliation compares the non-posting sales order header. When the order is billed in full, the
posted invoices are what the customer owes; once they equal the source, total and tax, with no credit,
return or refund adjustment in play, the order is matched on the invoices, and the sales order header is
kept in original_amounts. The shape the sales-order alignment write can fix (one invoice, no tax
anywhere, base currency) keeps its difference with that write required (decided by Aiden, 2026-10-22).
Synthetic data only.
"""

from decimal import Decimal

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_order_balance_reconciliation import evidence


def billed(source_total="1314.00", source_tax="209.80", *, invoices=(("5", "1314.00", "209.80", "1.09824"),)):
    source, target, config, refunds = evidence()
    source["orders"][0].update(total=source_total, included_tax_total=source_tax, additional_tax_total="0.00")
    target["orders"][0]["header"].update(total="1500.00", taxTotal="230.00", status={"id": "G"})
    ids = [identifier for identifier, *_ in invoices]
    refunds["target"].update(
        billing_ids=ids,
        billed_invoices={
            "complete": True,
            "ids": ids,
            "total": str(sum((Decimal(t) for _, t, _, _ in invoices), Decimal(0))),
            "tax": str(sum((Decimal(x) for _, _, x, _ in invoices), Decimal(0))),
            "exchange_rates": sorted({rate for *_, rate in invoices}),
        },
        invoice_credits={
            "complete": True,
            "credits": [],
            "total": "0",
            "tax": "0",
            "return_credits": {"complete": True, "credits": [], "total": "0", "tax": "0"},
        },
        tagged_credits=[],
        tax_adjustments=[],
    )
    return source, target, config, refunds


def test_posted_invoices_that_equal_the_source_match_a_stale_sales_order():
    source, target, config, refunds = billed()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert (result["status"], result["reason"]) == ("matched", "verified_adjustments_agree")
    assert result["amounts"]["order_total"] == {"source": "1314.00", "target": "1314.00", "delta": "0.00"}
    assert result["amounts"]["tax"] == {"source": "209.80", "target": "209.80", "delta": "0.00"}
    assert result["original_amounts"]["order_total"]["target"] == "1500.00"
    [adjustment] = result["adjustments"]
    assert adjustment["kind"] == "billed_invoices_match_source"
    assert (adjustment["invoices"], adjustment["total"], adjustment["tax"]) == (["5"], "1314.00", "209.80")
    assert "posting_reconciliation" not in result


def test_two_invoices_are_summed():
    source, target, config, refunds = billed(
        "2499.00", "499.80", invoices=(("5", "0.00", "0.00", "0.0918428"), ("6", "2499.00", "499.80", "0.0924702"))
    )
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "matched"


def test_a_zero_order_billed_at_zero_leaves_the_source_tax_to_the_zero_order_rule():
    source, target, config, refunds = billed("0.00", "4.00", invoices=(("5", "0.00", "0.00", "1"),))
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert [a["kind"] for a in result["adjustments"]] == ["billed_invoices_match_source", "zero_total_order_tax"]


def test_a_zero_order_where_money_was_taken_keeps_its_tax_difference():
    source, target, config, refunds = billed("0.00", "4.00", invoices=(("5", "0.00", "0.00", "1"),))
    source["orders"][0]["payment_total"] = "4.00"
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "difference"


def test_the_shape_the_sales_order_alignment_fixes_keeps_its_difference_with_that_write_required():
    source, target, config, refunds = billed("1499.00", "0.00", invoices=(("5", "1499.00", "0.00", "1"),))
    target["orders"][0]["header"].update(total="1659.00", taxTotal="0.00")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["posting_reconciliation"] == {
        "status": "matched",
        "source": "1499.00",
        "net_posting_total": "1499.00",
        "delta": "0.00",
        "basis": "billed_invoices_match_source",
        "sales_order_alignment": "required",
    }
    assert result["amounts"]["order_total"]["target"] == "1659.00"


def test_a_foreign_currency_order_of_that_shape_matches_since_the_alignment_cannot_reach_it():
    source, target, config, refunds = billed("42942.14", "0.00", invoices=(("5", "42942.14", "0.00", "0.0396271"),))
    target["orders"][0]["header"].update(total="43000.00", taxTotal="0.00")
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "matched"


@pytest.mark.parametrize(
    "change",
    [
        lambda s, t, r: t["orders"][0]["header"].update(status={"id": "E"}),  # partially billed
        lambda s, t, r: t["orders"][0]["header"].pop("status"),
        lambda s, t, r: r["target"].update(complete=False),
        lambda s, t, r: r["target"]["billed_invoices"].update(complete=False),
        lambda s, t, r: r["target"].pop("billed_invoices"),
        lambda s, t, r: r["target"].update(billing_ids=["5", "7"]),
        lambda s, t, r: r["target"]["billed_invoices"].update(total="1314.01"),
        lambda s, t, r: r["target"]["billed_invoices"].update(tax="209.79"),
        lambda s, t, r: r["target"]["invoice_credits"].update(complete=False),
        lambda s, t, r: r["target"]["invoice_credits"].update(credits=[{"id": "9"}]),
        lambda s, t, r: r["target"]["invoice_credits"]["return_credits"].update(complete=False),
        lambda s, t, r: r["target"]["invoice_credits"]["return_credits"].update(credits=[{"id": "9"}]),
        lambda s, t, r: r["target"].update(tagged_credits=[{"id": "9"}]),
        lambda s, t, r: r["target"].pop("tagged_credits"),
        lambda s, t, r: r["target"].update(tax_adjustments=[{"credit_memo_id": "9"}]),
    ],
)
def test_anything_the_evidence_does_not_prove_keeps_the_difference(change):
    source, target, config, refunds = billed()
    change(source, target, refunds)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] != "matched"
    assert not any(a.get("kind") == "billed_invoices_match_source" for a in result.get("adjustments") or [])


def test_refunds_that_differ_stay_a_difference_of_their_own():
    source, target, config, refunds = billed()
    refunds["target"]["amount"] = "5.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert {key for key, row in result["amounts"].items() if row["delta"] != "0.00"} == {"refunds"}


def test_an_order_that_already_matches_is_untouched():
    source, target, config, refunds = billed()
    target["orders"][0]["header"].update(total="1314.00", taxTotal="209.80")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert (result["status"], result["reason"]) == ("matched", "all_amounts_agree")
