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
            "account": {"id": "119"},
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

        # A credit memo's postings. Left as None, it is derived from the record above so a
        # test that edits the record does not silently leave the ledger describing something
        # else. Set it directly to describe a shape the record cannot express, such as tax
        # charged through the tax engine.
        self.ledger = None
        self.ledger_complete = True
        self.ledger_reads = 0

    AR_ACCOUNT = "119"

    def postings(self):
        if self.ledger is not None:
            return self.ledger
        memo = self.credit["id"]
        rows = [
            {
                "transaction": memo,
                "account": self.AR_ACCOUNT,
                "accountingbook": "1",
                "netamount": "-" + self.credit["total"],
            }
        ]
        rows.extend(
            {
                "transaction": memo,
                "account": line["account"]["id"],
                "accountingbook": "1",
                "netamount": line["amount"],
            }
            for line in self.credit["item"]["items"]
        )
        return rows

    async def request(self, method, path, **kwargs):
        if path == "/record/v1/creditmemo/3":
            self.calls += 1
            return deepcopy(self.credit)
        if "transactionaccountingline" in kwargs.get("body", {}).get("q", ""):
            self.calls += 1
            self.ledger_reads += 1
            rows = deepcopy(self.postings())
            return {
                "items": rows,
                "count": len(rows),
                "totalResults": len(rows),
                "hasMore": not self.ledger_complete,
            }
        return await super().request(method, path, **kwargs)


def reader_profile():
    return {**PROFILE, "subsidiary_id": "1", "tax_reversal_reason_ids": ["102"], "tax_accounts": ["90"]}


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


async def test_standard_allowance_credit_preserves_its_native_zero_tax_effect():
    reader = CreditReader()
    reader.requests[0]["reason_id"] = "3"
    reader.credit["item"]["items"][0].update(item={"id": "81"}, account={"id": "91"})
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["tax_adjustments"][0]["kind"] == "credit_memo"
    assert result["tax_adjustments"][0]["tax_amount"] == "0"
    assert result["tax_adjustments"][0]["amount"] == "578.38"


def test_standard_credit_explains_total_without_inventing_a_tax_reversal():
    s, t, c, r = balance_case()
    proof = r["target"]["tax_adjustments"][0]
    proof.update(kind="credit_memo", reason_id="3", tax_amount="0", item_accounts={"81": "91"})
    result = reconcile_order(s, t, c, refunds=r)
    assert result["amounts"]["order_total"]["delta"] == "0.00"
    assert result["amounts"]["tax"]["delta"] == "-20.00"
    assert result["status"] == "difference"
    s["orders"][0]["included_tax_total"] = "20"
    assert reconcile_order(s, t, c, refunds=r)["status"] == "matched"


# Framework Inc books refund tax through the tax engine, which NetSuite's REST layer does
# not surface: the item sublist carries neither grossAmt nor tax1Amt, while the ledger and
# SuiteQL both hold the split. Numbers are production credit memo CM11657 (internal id
# 15778068) for order R013617167: 433.80 gross, 400.00 into 40100 Sales Returns &
# Allowances, 33.80 into 21501 US Sales Taxes.
US_TAX_ACCOUNT = "210"
US_NET_ACCOUNT = "783"
US_CREDIT_TOTAL = "433.80"


class LedgerCreditReader(CreditReader):
    def __init__(self):
        super().__init__()
        self.requests[0]["reason_id"] = "6"
        self.requests[0]["amount"] = US_CREDIT_TOTAL
        self.record["total"] = US_CREDIT_TOTAL
        self.record["apply"]["items"] = [{"apply": True, "doc": {"id": "3"}, "line": 0, "amount": US_CREDIT_TOTAL}]
        self.credit.update(total=US_CREDIT_TOTAL, applied=US_CREDIT_TOTAL, taxTotal="33.80")
        self.credit["item"]["items"] = [
            {
                "line": 1,
                "item": {"id": "1603"},
                "account": {"id": US_NET_ACCOUNT},
                "itemType": {"id": "NonInvtPart"},
                "amount": "400.00",
            }
        ]
        self.ledger = [
            {"transaction": "3", "account": "119", "accountingbook": "1", "netamount": "-" + US_CREDIT_TOTAL},
            {"transaction": "3", "account": US_TAX_ACCOUNT, "accountingbook": "1", "netamount": "33.80"},
            {"transaction": "3", "account": US_NET_ACCOUNT, "accountingbook": "1", "netamount": "400.00"},
        ]


def ledger_profile():
    return {**reader_profile(), "tax_accounts": [US_TAX_ACCOUNT]}


async def test_us_credit_memo_proves_its_tax_split_from_the_ledger():
    """The REST record hides the split; the ledger holds it. Today this returns nothing."""
    reader = LedgerCreditReader()
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert reader.ledger_reads == 1, "the proof must have consulted the ledger, not the record header"
    assert result["amount"] == Decimal(US_CREDIT_TOTAL)
    proof = result["tax_adjustments"][0]
    assert proof["kind"] == "credit_memo"
    assert proof["amount"] == US_CREDIT_TOTAL
    assert proof["tax_amount"] == "33.80"
    # The verdict carries the netting it came from, so a reviewer can re-derive it.
    assert proof["ledger_accounts"] == {"119": "-433.80", US_NET_ACCOUNT: "400.00", US_TAX_ACCOUNT: "33.80"}


@pytest.mark.parametrize(
    "break_ledger",
    [
        pytest.param(lambda r: r.ledger.append(dict(r.ledger[1], netamount="1.00")), id="unbalanced"),
        pytest.param(
            lambda r: (r.ledger[0].update(netamount="-500.00"), r.ledger[2].update(netamount="466.20")),
            id="balances_but_receivable_is_not_the_refund",
        ),
        pytest.param(lambda r: r.ledger[2].update(accountingbook="2"), id="two_books"),
        pytest.param(lambda r: r.ledger[2].update(accountingbook=None), id="a_posting_with_no_book"),
        pytest.param(
            lambda r: r.ledger.append(
                {"transaction": "3", "account": None, "accountingbook": "1", "netamount": "5.00"}
            ),
            id="amount_attributed_to_no_account",
        ),
        pytest.param(lambda r: setattr(r, "ledger_complete", False), id="truncated"),
        pytest.param(lambda r: r.ledger.clear(), id="empty"),
    ],
)
async def test_a_ledger_that_does_not_account_for_the_refund_proves_nothing(break_ledger):
    reader = LedgerCreditReader()
    break_ledger(reader)
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["amount"] == Decimal(US_CREDIT_TOTAL)
    assert result["tax_adjustments"] == []


async def test_tax_posted_by_an_item_line_is_still_tax():
    """Framework BV reverses VAT as an ordinary item line into the VAT account, leaving the
    record header at zero tax. The ledger is the only place that is visible."""
    reader = CreditReader()
    assert reader.credit["taxTotal"] == "0"
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    proof = result["tax_adjustments"][0]
    assert proof["kind"] == "tax_reversal"
    assert proof["tax_amount"] == "578.38"


async def test_the_split_is_read_in_the_order_currency_not_the_subsidiary_base():
    """transactionaccountingline posts in the subsidiary's base currency while the refund is
    stated in the order's own. Reading the base-currency columns rejects every foreign
    order, so the proof must take the line's transaction-currency amount."""
    reader = LedgerCreditReader()
    for row in reader.ledger:
        # Base-currency columns alongside, at a different rate. Reading these would make the
        # receivable disagree with the refund and discard a correct credit memo.
        row["debit"] = "999.99"
        row["credit"] = "999.99"
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["tax_adjustments"][0]["tax_amount"] == "33.80"


async def test_an_inventory_return_posts_its_own_matched_pair_without_disturbing_the_refund():
    """A return books inventory against cost of goods. That pair never touches the
    receivable, and demanding every debit add up to the refund would reject the entry."""
    reader = LedgerCreditReader()
    reader.ledger.extend(
        [
            {"transaction": "3", "account": "212", "accountingbook": "1", "netamount": "120.00"},
            {"transaction": "3", "account": "230", "accountingbook": "1", "netamount": "-120.00"},
        ]
    )
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    proof = result["tax_adjustments"][0]
    assert proof["amount"] == US_CREDIT_TOTAL
    assert proof["tax_amount"] == "33.80"


async def test_netsuite_own_tax_total_must_agree_when_the_credit_is_not_a_reversal():
    """The header total comes from NetSuite's tax engine, independent of the account list we
    declare. Without it an incomplete tax_accounts silently understates tax and still
    balances."""
    reader = LedgerCreditReader()
    reader.credit["taxTotal"] = "30.00"
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["tax_adjustments"] == []


async def test_a_tax_account_missing_from_the_profile_is_caught_not_understated():
    reader = LedgerCreditReader()
    profile = {**ledger_profile(), "tax_accounts": ["999"]}
    result = await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=profile)
    assert result["tax_adjustments"] == []


async def test_a_credit_memo_with_no_tax_line_must_prove_it_rather_than_omit_it():
    """An absent taxTotal is missing evidence, not a zero. A subtotal equal to the total is
    what proves there was nowhere for tax to be."""
    reader = LedgerCreditReader()
    del reader.credit["taxTotal"]
    reader.credit.update(total="400.00", applied="400.00", subtotal="400.00")
    reader.requests[0]["amount"] = "400.00"
    reader.record["total"] = "400.00"
    reader.record["apply"]["items"] = [{"apply": True, "doc": {"id": "3"}, "line": 0, "amount": "400.00"}]
    reader.ledger = [
        {"transaction": "3", "account": "119", "accountingbook": "1", "netamount": "-400.00"},
        {"transaction": "3", "account": US_NET_ACCOUNT, "accountingbook": "1", "netamount": "400.00"},
    ]
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["tax_adjustments"][0]["tax_amount"] == "0"

    # Same record, but the subtotal no longer accounts for the whole total: tax is unproven.
    reader = LedgerCreditReader()
    del reader.credit["taxTotal"]
    reader.credit["subtotal"] = "400.00"
    assert (
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile())
    )["tax_adjustments"] == []


@pytest.mark.parametrize(
    "break_record",
    [
        pytest.param(lambda r: r.credit.update(taxTotal=None), id="tax_total_present_but_null"),
        pytest.param(lambda r: r.credit["item"]["items"][0].update(amount="380.00"), id="lines_do_not_sum_to_net"),
        pytest.param(lambda r: r.ledger[1].update(netamount=None), id="posting_line_with_no_amount"),
    ],
)
async def test_incomplete_record_evidence_proves_nothing(break_record):
    reader = LedgerCreditReader()
    break_record(reader)
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["amount"] == Decimal(US_CREDIT_TOTAL)
    assert result["tax_adjustments"] == []


async def test_a_reversal_whose_tax_engine_also_computed_tax_is_not_a_clean_reversal():
    """A reversal posts everything to a tax account by hand; a non-zero engine total means
    the record is saying two different things about the same money."""
    reader = CreditReader()
    assert reader.requests[0]["reason_id"] == "102"
    reader.credit["taxTotal"] = "10.00"
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["tax_adjustments"] == []


async def test_a_reversals_lines_must_account_for_the_refund_too():
    """The line-sum invariant used to apply to both kinds. When it came back it landed only
    in the ordinary branch, which is the same asymmetry the round before had."""
    reader = CreditReader()
    reader.credit["item"]["items"][0]["amount"] = "500.00"
    reader.ledger = [
        {"transaction": "3", "account": "119", "accountingbook": "1", "netamount": "-578.38"},
        {"transaction": "3", "account": "90", "accountingbook": "1", "netamount": "578.38"},
    ]
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=reader_profile()
    )
    assert result["tax_adjustments"] == []


async def test_one_item_posting_to_two_accounts_is_refused_not_quietly_halved():
    reader = LedgerCreditReader()
    reader.credit["item"]["items"] = [
        {
            "line": 1,
            "item": {"id": "1603"},
            "account": {"id": US_NET_ACCOUNT},
            "itemType": {"id": "NonInvtPart"},
            "amount": "200.00",
        },
        {
            "line": 2,
            "item": {"id": "1603"},
            "account": {"id": "700"},
            "itemType": {"id": "NonInvtPart"},
            "amount": "200.00",
        },
    ]
    reader.ledger = [
        {"transaction": "3", "account": "119", "accountingbook": "1", "netamount": "-" + US_CREDIT_TOTAL},
        {"transaction": "3", "account": US_TAX_ACCOUNT, "accountingbook": "1", "netamount": "33.80"},
        {"transaction": "3", "account": US_NET_ACCOUNT, "accountingbook": "1", "netamount": "200.00"},
        {"transaction": "3", "account": "700", "accountingbook": "1", "netamount": "200.00"},
    ]
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["tax_adjustments"] == []


async def test_a_line_naming_an_account_the_ledger_never_posted_to_proves_nothing():
    """Totals alone would pass. The evidence would then attribute money to an account the
    books never saw, which is precisely what ledger_accounts exists to make checkable."""
    reader = LedgerCreditReader()
    reader.credit["item"]["items"][0]["account"] = {"id": "999"}
    result = await collect_refunds(
        reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=ledger_profile()
    )
    assert result["tax_adjustments"] == []


async def test_a_subsidiary_that_has_not_declared_its_tax_accounts_stays_as_it_was():
    """Framework BV carries a refund profile today but no tax_accounts. Inferring them from
    the tax-item map would move it onto the ledger proof on deploy day, with nobody deciding
    to. Refusing leaves its reconciliation untouched until someone declares them."""
    reader = LedgerCreditReader()
    profile = {k: v for k, v in ledger_profile().items() if k != "tax_accounts"}
    result = await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=profile)
    assert result["amount"] == Decimal(US_CREDIT_TOTAL)
    assert result["tax_adjustments"] == []
    assert reader.ledger_reads == 0, "and it should not spend a provider call to find that out"
