"""Two reconciliation rules decided by Aiden on 2026-10-22, over Framework's open backlog.

1. A canceled order whose NetSuite sales order is closed (or cancelled) with nothing billed, every
   customer deposit fully applied and nothing still paid in the source is settled: the books are
   right, and the closed sales order's header total is not an amount anyone owes. About 35 open
   cases (e.g. R110074126: the $100 pre-order deposit refunded, SO closed).
2. An order whose source total and NetSuite total are both zero has no tax to compare: Solidus keeps
   the pre-adjustment tax on an "Adjusted to zero" order while NetSuite carries 0.00 (26 open cases,
   e.g. R627831434: Solidus tax 0.88, NetSuite 0.00 / 0.00).

Refunds are still compared exactly in both, and every other amount keeps its plain comparison.
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_order_balance_reconciliation import evidence


def canceled():
    """R110074126's shape: canceled in Solidus with its payment voided, the $100 deposit refunded,
    and the NetSuite sales order closed with nothing billed."""
    source, target, config, refunds = evidence()
    order = source["orders"][0]
    order.update(state="canceled", payment_state="void", payment_total="0.0", total="1162.96")
    order.update(included_tax_total="4.31", additional_tax_total="0.00")
    header = target["orders"][0]["header"]
    header.update(total="1158.65", taxTotal="0.00", orderStatus={"id": "H", "refName": "Closed"})
    for side in ("source", "target"):
        refunds[side]["amount"] = "100.00"
    refunds["target"].update(billing_ids=[], deposits=[{"id": "15833387", "total": "100.00", "status": "C"}])
    return source, target, config, refunds


def test_a_canceled_order_closed_unbilled_with_its_deposit_refunded_is_settled():
    source, target, config, refunds = canceled()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "verified_adjustments_agree"
    assert {k: v["delta"] for k, v in result["amounts"].items()} == {
        "order_total": "0.00",
        "tax": "0.00",
        "refunds": "0.00",
    }
    (adjustment,) = result["adjustments"]
    assert adjustment["kind"] == "canceled_order_settled" and adjustment["status"] == "verified"
    assert adjustment["sales_order_status"] == "H"
    assert adjustment["deposits"] == [{"id": "15833387", "total": "100.00", "status": "C"}]
    # The header comparison it replaced stays on record.
    assert result["original_amounts"]["order_total"] == {"source": "1162.96", "target": "1158.65", "delta": "4.31"}


def test_a_cancelled_sales_order_counts_as_closed():
    source, target, config, refunds = canceled()
    target["orders"][0]["header"]["orderStatus"] = {"id": "C", "refName": "Cancelled"}
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "matched"


@pytest.mark.parametrize(
    "change",
    [
        # Still active in the source.
        lambda s, t, r: s["orders"][0].update(state="complete"),
        # NetSuite still has an open sales order (pending approval / pending fulfillment).
        lambda s, t, r: t["orders"][0]["header"].update(orderStatus={"id": "A"}),
        lambda s, t, r: t["orders"][0]["header"].update(orderStatus={"id": "B"}),
        lambda s, t, r: t["orders"][0]["header"].pop("orderStatus"),
        # Something was billed: an invoice or a cash sale is a real amount owed.
        lambda s, t, r: r["target"].update(billing_ids=["16000001"]),
        # Billing was never read: unknown is not "nothing billed".
        lambda s, t, r: r["target"].pop("billing_ids"),
        # A deposit NetSuite still holds (deposited, not applied).
        lambda s, t, r: r["target"]["deposits"][0].update(status="B"),
        # Deposits were not read for this order.
        lambda s, t, r: r["target"].pop("deposits"),
        lambda s, t, r: r["target"].update(deposits="unread"),
        # The source still holds a payment (refund not finished).
        lambda s, t, r: s["orders"][0].update(payment_total="100.0"),
        lambda s, t, r: s["orders"][0].pop("payment_total"),
        # The refund read is not this order's complete read.
        lambda s, t, r: r["target"].update(complete=False),
    ],
)
def test_anything_short_of_settled_keeps_the_plain_comparison(change):
    source, target, config, refunds = canceled()
    change(source, target, refunds)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] != "matched"
    assert not any(a.get("kind") == "canceled_order_settled" for a in result.get("adjustments") or [])


def test_refunds_are_still_compared_on_a_settled_canceled_order():
    source, target, config, refunds = canceled()
    refunds["target"]["amount"] = "0.00"  # the deposit was moved, not refunded, but Solidus refunded it
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference" and result["amounts"]["refunds"]["delta"] == "100.00"


def zero_order():
    """R627831434's shape: adjusted to zero in Solidus, which still reports its 0.88 tax."""
    source, target, config, refunds = evidence()
    source["orders"][0].update(total="0.00", included_tax_total="0.88", additional_tax_total="0.00")
    target["orders"][0]["header"].update(total="0.00", taxTotal="0.00")
    return source, target, config, refunds


def test_an_order_zero_on_both_sides_has_no_tax_to_compare():
    source, target, config, refunds = zero_order()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["amounts"]["tax"]["delta"] == "0.00"
    (adjustment,) = result["adjustments"]
    assert adjustment == {
        "kind": "zero_total_order_tax",
        "status": "verified",
        "basis": "the source and NetSuite order totals are both zero, so the source's tax is not owed",
        "source_tax": "0.88",
    }
    assert result["original_amounts"]["tax"]["delta"] == "0.88"


@pytest.mark.parametrize(
    "change",
    [
        lambda s, t: s["orders"][0].update(total="5.00"),  # the source still charges something
        lambda s, t: t["orders"][0]["header"].update(total="5.00"),  # NetSuite still bills something
        lambda s, t: t["orders"][0]["header"].update(taxTotal="0.50"),  # NetSuite carries tax
    ],
)
def test_a_zero_order_rule_needs_both_totals_zero_and_no_netsuite_tax(change):
    source, target, config, refunds = zero_order()
    change(source, target)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] != "matched"


def test_a_refund_difference_still_shows_on_a_zero_order():
    source, target, config, refunds = zero_order()
    refunds["target"]["amount"] = "10.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference" and result["amounts"]["refunds"]["delta"] == "-10.00"


def test_neither_rule_touches_an_order_that_already_matches():
    source, target, config, refunds = evidence()
    plain = reconcile_order(deepcopy(source), deepcopy(target), config, refunds=deepcopy(refunds))
    assert plain["status"] == "matched" and "adjustments" not in plain


@pytest.mark.parametrize(
    "fields,matched",
    [
        ({"payment_total": "0.0"}, True),
        ({"payment_total": "2.00"}, False),  # money taken: the tax may have been collected
        ({"total_applicable_store_credit": "2.00"}, False),
        ({"payments": [{"amount": "2.00", "state": "completed"}]}, False),
        ({"payments": [{"amount": "2.00"}]}, False),  # a payment of unknown state counts as taken
        ({"payments": [{"amount": "2.00", "state": "void"}]}, True),
        ({"payments": [{"amount": "x", "state": "void"}]}, False),  # unreadable is not zero
    ],
)
def test_a_zero_order_that_took_money_keeps_its_tax_comparison(fields, matched):
    source, target, config, refunds = zero_order()
    source["orders"][0].update(fields)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert (result["status"] == "matched") is matched
