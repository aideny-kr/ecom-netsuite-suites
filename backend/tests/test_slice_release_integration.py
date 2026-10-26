"""Interactions between the rules shipped together on 2026-10-26 (C09, C15, C24).

Each rule was built and reviewed alone; these tests pin how they combine. Every case is
synthetic. Review packet on the integrated branch, round 1: R3, R4.
"""

from app.services.transaction_ops import netsuite_refunds
from app.services.transaction_ops.netsuite_refunds import collect_refunds
from tests.test_netsuite_tagged_refund_credits import CREDIT, ORDER, REFERENCE, TaggedReader


async def test_a_hand_booked_credit_is_never_one_of_the_orders_return_credits(monkeypatch):
    """R3: C09 attaches a tagged credit through a synthetic RA -> credit edge. C15 then saw it as a
    return credit, whose origin (the paying customer refund) is not the RA, so the whole return tier
    became unproven and a genuine RA credit stopped counting. Its money is the refund (C09); only the
    order's own return credits go to the totals read."""
    from tests.test_netsuite_refund_graph import edge
    from tests.test_netsuite_tagged_refund_credits import RETURN

    seen = []
    real = netsuite_refunds.read_invoice_credits

    async def spy(*args, **kwargs):
        seen.append(list(kwargs.get("returns") or ()))
        return await real(*args, **kwargs)

    monkeypatch.setattr(netsuite_refunds, "read_invoice_credits", spy)
    reader = TaggedReader()  # RA closed: the tagged credit is accepted
    genuine = "1011"
    reader.edges.append({**edge(RETURN, genuine, "RtnAuth", "CustCred"), "previousstatus": "H"})  # RA closed
    result = await collect_refunds(reader, ORDER, "1", "1", order_reference=REFERENCE)
    assert result["tagged_credits"] == [CREDIT]
    assert seen == [[genuine]]


def test_a_tax_zero_proven_from_netsuite_lets_a_return_credit_explain_the_order():
    """R4: C24 proves an absent taxTotal is zero, but C15 compared the invoices with the header's
    unread tax, so a return credit could never explain an order whose tax REST left out."""
    from app.services.transaction_ops.order_reconciliation import reconcile_order
    from tests.test_order_balance_reconciliation import returned

    source, target, config, refunds = returned(("0.00", "0.00"), ("500.00", "0.00"), returns=[("500.00", "0.00")])
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "matched"
    header = target["orders"][0]["header"]
    del header["taxTotal"]
    header["subtotal"] = "500.00"
    target["orders"][0]["native_header"] = {"id": header["id"], "taxtotal": "0", "total": "500"}
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["target_tax_basis"] == "native_taxtotal"
    assert result["status"] == "matched"
