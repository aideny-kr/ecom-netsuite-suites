"""Only scoped native tax-credit evidence may explain an order/tax variance."""

from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.netsuite_refunds import collect_refunds
from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_netsuite_custom_refunds import REFERENCE, CustomReader
from tests.test_order_balance_reconciliation import evidence

PROFILE = {
    "schema_version": 1,
    "account_id": "123-sb1",
    "subsidiary_id": "5",
    "tax_reversal_reason_ids": ["70"],
    "tax_item_accounts": {"80": "90"},
}


def balance_case():
    source, target, config, refunds = evidence()
    source["orders"][0].update(total="100", included_tax_total="0", additional_tax_total="0")
    config["mapping_json"]["refund_adjustments"] = deepcopy(PROFILE)
    config["netsuite_connection_id"] = "native-connection"
    for side in refunds.values():
        side.update(amount="20", refund_count=1)
    refunds["source"].update(events_complete=True, events=[{"id": "30", "payment_number": "PAY123", "amount": "20"}])
    refunds["target"].update(
        provider="netsuite",
        account_id="123-sb1",
        subsidiary_id="5",
        connection_id="native-connection",
        tax_adjustments=[
            {
                "kind": "tax_reversal",
                "request_id": "20",
                "source_refund_id": "30",
                "payment_number": "PAY123",
                "credit_memo_id": "3",
                "refund_id": "4",
                "order_record_id": "77",
                "amount": "20",
                "reason_id": "70",
                "item_accounts": {"80": "90"},
            }
        ],
    )
    return source, target, config, refunds


def test_verified_vat_credit_matches_and_preserves_original_order_tax_and_refund():
    s, t, c, r = balance_case()
    before = deepcopy((s, t, c, r))
    result = reconcile_order(s, t, c, refunds=r)
    assert result["status"] == "matched"
    assert result["reason"] == "verified_adjustments_agree"
    assert result["amounts"]["order_total"] == {"source": "100.00", "target": "100.00", "delta": "0.00"}
    assert result["amounts"]["tax"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}
    assert result["amounts"]["refunds"]["target"] == "20.00"
    assert result["original_amounts"]["order_total"]["target"] == "120.00"
    assert result["original_amounts"]["tax"]["target"] == "20.00"
    assert result["adjustments"][0]["credit_memo_id"] == "3"
    assert (s, t, c, r) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda s, t, c, r: c["mapping_json"].pop("refund_adjustments"),
        lambda s, t, c, r: c["mapping_json"]["refund_adjustments"].update(account_id="999"),
        lambda s, t, c, r: r["source"].update(events_complete=False),
        lambda s, t, c, r: r["source"]["events"][0].update(id="99"),
        lambda s, t, c, r: r["source"]["events"][0].update(payment_number="OTHER"),
        lambda s, t, c, r: r["source"]["events"][0].update(amount="19"),
        lambda s, t, c, r: r["source"]["events"].append(deepcopy(r["source"]["events"][0])),
        lambda s, t, c, r: r["target"].update(complete=False),
        lambda s, t, c, r: r["target"].update(connection_id="other"),
        lambda s, t, c, r: r["target"].update(account_id="other"),
        lambda s, t, c, r: r["target"]["tax_adjustments"][0].update(order_record_id="99"),
        lambda s, t, c, r: r["target"]["tax_adjustments"][0].update(reason_id="71"),
        lambda s, t, c, r: r["target"]["tax_adjustments"][0].update(item_accounts={"80": "91"}),
        lambda s, t, c, r: r["target"]["tax_adjustments"].append(deepcopy(r["target"]["tax_adjustments"][0])),
    ],
)
def test_equal_refund_amount_alone_cannot_explain_tax(change):
    s, t, c, r = balance_case()
    change(s, t, c, r)
    assert reconcile_order(s, t, c, refunds=r)["status"] != "matched"


def test_already_updated_order_is_not_reduced_twice():
    s, t, c, r = balance_case()
    t["orders"][0]["header"].update(total="100", taxTotal="0")
    result = reconcile_order(s, t, c, refunds=r)
    assert result["status"] == "matched"
    assert result["amounts"]["order_total"]["target"] == "100.00"


def test_real_residual_tax_variance_is_not_rounded_away():
    s, t, c, r = balance_case()
    t["orders"][0]["header"]["taxTotal"] = "20.01"
    result = reconcile_order(s, t, c, refunds=r)
    assert result["status"] == "difference"
    assert result["amounts"]["tax"]["delta"] == "-0.01"


class CreditReader(CustomReader):
    def __init__(self):
        super().__init__()
        self.credit = {
            "id": "3",
            "currency": {"id": "1"},
            "subsidiary": {"id": "1"},
            "custbody_fw_order_number": REFERENCE,
            "total": "578.38",
            "taxTotal": "0",
            "applied": "578.38",
            "unapplied": "0",
            "lastModifiedDate": "2026-09-01T00:00:00Z",
            "item": {
                "items": [
                    {
                        "line": 1,
                        "item": {"id": "80"},
                        "account": {"id": "90"},
                        "itemType": {"id": "NonInvtPart"},
                        "amount": "578.38",
                        "grossAmt": "578.38",
                        "tax1Amt": "0",
                    }
                ],
                "totalResults": 1,
                "count": 1,
                "offset": 0,
                "hasMore": False,
            },
        }

    async def request(self, method, path, **kwargs):
        if path == "/record/v1/creditmemo/3":
            self.calls += 1
            return deepcopy(self.credit)
        return await super().request(method, path, **kwargs)


def reader_profile():
    return {**PROFILE, "subsidiary_id": "1", "tax_reversal_reason_ids": ["102"]}


async def test_native_credit_lines_prove_tax_effect_separate_from_refund_money():
    result = await collect_refunds(
        CreditReader(), "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["amount"] == Decimal("578.38")
    assert result["tax_adjustments"][0]["amount"] == "578.38"
    assert result["tax_adjustments"][0]["item_accounts"] == {"80": "90"}


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(id="99"),
        lambda c: c.update(custbody_fw_order_number="R999999999"),
        lambda c: c.update(total="578.39"),
        lambda c: c.update(unapplied="1"),
        lambda c: c.update(taxTotal="1"),
        lambda c: c["item"].update(hasMore=True),
        lambda c: c["item"]["items"][0].update(account={"id": "91"}),
        lambda c: c["item"]["items"][0].update(item={"id": "81"}),
        lambda c: c["item"]["items"][0].update(amount="578.37"),
    ],
)
async def test_unknown_credit_effect_preserves_refund_without_fabricating_tax_adjustment(change):
    reader = CreditReader()
    change(reader.credit)
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["amount"] == Decimal("578.38")
    assert result["tax_adjustments"] == []


async def test_refund_split_between_credit_and_other_document_does_not_prove_full_credit_refund():
    from tests.test_netsuite_refund_graph import edge

    reader = CreditReader()
    reader.edges.extend(
        [
            edge("1", "5", "SalesOrd", "CustDep"),
            edge("5", "6", "CustDep", "DepAppl"),
            edge("4", "6", "CustRfnd", "DepAppl"),
        ]
    )
    reader.record["apply"]["items"] = [
        {"apply": True, "doc": {"id": "3"}, "line": 0, "amount": "300"},
        {"apply": True, "doc": {"id": "6"}, "line": 1, "amount": "278.38"},
    ]
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["amount"] == Decimal("578.38")
    assert result["tax_adjustments"] == []
