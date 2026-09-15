from copy import deepcopy

import pytest

from app.services.transaction_ops.credit_reallocation import build_intent
from tests.test_line_evidence import revision_inputs


def fixture():
    source, evidence = revision_inputs()
    source.update(payment_state="paid", payment_total="1320")
    source["line_items"][0]["adjustments"] = [
        {
            "id": "3",
            "source_type": "Spree::TaxRate",
            "adjustable_type": "Spree::LineItem",
            "adjustable_id": "101",
            "amount": "120",
            "finalized": False,
        }
    ]
    for d in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        d["line_evidence"]["lines"][0]["custcol_fw_vat_amount"] = "160"
    invoice = deepcopy(evidence["sections"]["posting_documents"][0])
    invoice.update(
        currency={"id": "1"},
        subsidiary={"id": "1"},
        entity={"id": "7"},
        account={"id": "11"},
        taxItem={"id": "61"},
        exchangeRate="1",
        amountPaid="1760",
        amountRemaining="0",
        createdFrom={"id": "10"},
    )
    credit = {
        "id": "30",
        "currency": {"id": "1"},
        "subsidiary": {"id": "1"},
        "entity": {"id": "7"},
        "account": {"id": "11"},
        "exchangeRate": "1",
        "custbody_fw_order_number": "R123",
        "total": "440",
        "subtotal": "440",
        "applied": "440",
        "unapplied": "0",
        "isTaxable": False,
        "taxRate": "0",
        "lastModifiedDate": "version-credit",
        "postingPeriod": {"id": "90"},
        "line_evidence": {
            "complete": True,
            "lines": [
                {
                    "line": 1,
                    "lineUniqueKey": "100",
                    "item": {"id": "4"},
                    "itemType": {"id": "NonInvtPart"},
                    "quantity": "1",
                    "amount": "440",
                    "rate": "440",
                    "isTaxable": False,
                }
            ],
        },
    }
    refund = {"id": "31", "currency": {"id": "1"}, "subsidiary": {"id": "1"}, "exchangeRate": "1", "total": "440"}
    support = {
        "invoice": invoice,
        "credit": credit,
        "refund": refund,
        "currency": {"id": "1", "symbol": "USD", "currencyPrecision": 2},
        "tax_item": {"id": "61", "isInactive": False, "taxAgency": {"id": "8"}},
        "item": {"id": "4", "isInactive": False, "incomeAccount": {"id": "12"}},
        "period": {"id": "90", "closed": False, "arLocked": False, "allLocked": False},
        "ar_account": "11",
        "offset_account": "12",
        "tax_account": "13",
        "book": "1",
        "refund_graph": {
            "complete": True,
            "refund_count": 1,
            "record_ids": ["31"],
            "amount": "440",
            "request_links": [{"stage": "refund_verified", "credit_memo_id": "30", "refund_id": "31", "amount": "440"}],
        },
        "credit_gl": {
            "complete": True,
            "rows": [
                {"account": "12", "accountingbook": "1", "debit": "440", "credit": None},
                {"account": "11", "accountingbook": "1", "debit": None, "credit": "440"},
            ],
        },
        "invoice_gl": {
            "complete": True,
            "rows": [
                {"account": "13", "accountingbook": "1", "debit": None, "credit": "160"},
                {"account": "11", "accountingbook": "1", "debit": "1760", "credit": None},
                {"account": "14", "accountingbook": "1", "debit": None, "credit": "1600"},
            ],
        },
        "observed_at": "observation-time",
    }
    review = {
        "scope": {"subsidiary_id": "1", "netsuite_account_id": "test-account"},
        "configuration_status": "scoped_configuration_found",
        "connection_active": True,
        "native_mcp_connector_id": "connector",
        "netsuite_connection_id": "connection",
        "config_id": "config",
    }
    return source, review, evidence, support


def test_intent_preserves_credit_refund_gross_and_requires_further_validation():
    source, review, evidence, support = fixture()
    original = deepcopy(support)
    intent = build_intent("tenant", "case", source, review, evidence, support)
    assert intent is not None
    assert intent["expected_after"]["total"] == "440"
    assert intent["expected_after"]["taxTotal"] == "40"
    assert intent["proposed_fields"]["item"]["items"] == [
        {"line": 1, "rate": 400.0, "amount": 400.0, "isTaxable": True}
    ]
    assert intent["proposed_fields"]["taxRate"] == 10.0
    assert intent["financial_write_authorized"] is False
    assert intent["status"] == "intent_requires_schema_policy_and_preflight_validation"
    assert "unfrozen" in intent["approval_basis"]
    assert support == original


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_refund",
        "partial_graph",
        "wrong_credit",
        "native_tax_disagrees",
        "wrong_account",
        "closed_period",
        "partial_gl",
        "changed_quantity",
        "different_currency",
        "missing_line_tax",
    ],
)
def test_ineligible_or_incomplete_evidence_never_yields_an_intent(change):
    source, review, evidence, support = fixture()
    if change == "duplicate_refund":
        support["refund_graph"]["refund_count"] = 2
    elif change == "partial_graph":
        support["refund_graph"]["complete"] = False
    elif change == "wrong_credit":
        support["refund_graph"]["request_links"][0]["credit_memo_id"] = "99"
    elif change == "native_tax_disagrees":
        support["credit"]["taxTotal"] = "40"
    elif change == "wrong_account":
        support["item"]["incomeAccount"]["id"] = "99"
    elif change == "closed_period":
        support["period"]["closed"] = True
    elif change == "partial_gl":
        support["credit_gl"]["complete"] = False
    elif change == "changed_quantity":
        source["line_items"][0]["quantity"] = "2"
    elif change == "different_currency":
        support["refund"]["currency"]["id"] = "2"
    else:
        source["line_items"][0].pop("adjustments")
    assert build_intent("tenant", "case", source, review, evidence, support) is None


async def test_discovery_reuses_postings_but_requires_current_complete_refund_ownership(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import credit_reallocation as mod

    source, review, evidence, support = fixture()
    invoice = support["invoice"]
    invoice["record_type"] = "invoice"
    support["credit"]["record_type"] = "creditmemo"
    support["refund"]["record_type"] = "customerrefund"
    evidence["sections"].update(
        posting_documents=[invoice],
        related_refund_documents={"documents": [support["credit"], support["refund"]], "complete": False},
        gl={str(invoice["id"]): support["invoice_gl"], "30": support["credit_gl"]},
        taxItem=[support["tax_item"]],
    )
    reader = AsyncMock()
    reader.request.side_effect = [support["currency"], support["item"], support["period"]]

    @asynccontextmanager
    async def factory(*args, **kwargs):
        assert kwargs["max_api_calls"] == 24
        yield reader

    graph = AsyncMock(return_value={k: v for k, v in support["refund_graph"].items() if k != "complete"})
    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", factory)
    monkeypatch.setattr("app.services.transaction_ops.netsuite_refunds.collect_refunds", graph)
    actual = await mod.collect_support(None, "tenant", source, review, evidence)
    assert actual["refund_graph"]["complete"] is True
    graph.assert_awaited_once()
    assert all(call.args[0] == "GET" for call in reader.request.await_args_list)
    intent = mod.build_intent("tenant", "case", source, review, evidence, actual)
    assert intent and intent["expected_after"]["taxTotal"] == "40"
    summary = mod.solution_summary(intent)
    assert summary["executable"] is False and summary["financial_write_authorized"] is False
    assert summary["sales_order_id"] == "10"
    # Historical native leads cannot stand in for a failed ownership read.
    graph.side_effect = ValueError("refund_allocation_ambiguous")
    with pytest.raises(ValueError, match="refund_allocation_ambiguous"):
        await mod.collect_support(None, "tenant", source, review, evidence)


async def test_discovery_does_not_read_for_unrelated_tax_only_cases(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.transaction_ops.credit_reallocation import collect_support

    source, review, evidence, _ = fixture()
    evidence["sections"]["posting_documents"] = []
    reader = MagicMock(side_effect=AssertionError("unnecessary native read"))
    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", reader)
    assert await collect_support(None, "tenant", source, review, evidence) is None
    reader.assert_not_called()


def test_dependent_sales_order_intent_uses_native_line_keys_and_preserves_posting_dependency():
    from app.services.transaction_ops.source_line_alignment import build_intent as align

    source, review, evidence, support = fixture()
    credit = build_intent("tenant", "case", source, review, evidence, support)
    evidence["sections"]["sales_order"]["line_evidence"]["lines"][0]["lineUniqueKey"] = "120"
    intent = align(source, evidence, credit)
    assert intent and intent["record_type"] == "salesorder"
    assert intent["depends_on"] == {"kind": "credit_tax_reallocation", "record_id": "30", "required_status": "verified"}
    assert intent["expected_after"] == {"subtotal": "1200", "taxTotal": "120", "total": "1320"}
    assert intent["proposed_fields"]["item"]["items"][0]["rate"] == "1200"
    assert intent["proposed_fields"]["item"]["items"][0]["custcol_fw_vat_amount"] == "120"
    assert "quantity" not in intent["proposed_fields"]["item"]["items"][0]
    assert intent["executable"] is False
    assert align(source, evidence, {**credit, "sales_order_id": "999"}) is None
    source["line_items"][0]["adjustments"] = []
    assert align(source, evidence, credit) is None


def test_tax_only_refund_requires_native_amount_preview_never_invents_an_infinite_rate():
    source, review, evidence, support = fixture()
    source.update(total="1720", item_total="1600", payment_total="1720")
    source["line_items"][0]["price"] = "1600"
    credit = support["credit"]
    for key in ("total", "subtotal", "applied"):
        credit[key] = "40"
    credit["line_evidence"]["lines"][0].update(rate="40", amount="40")
    support["refund"]["total"] = "40"
    support["refund_graph"]["amount"] = "40"
    support["refund_graph"]["request_links"][0]["amount"] = "40"
    support["credit_gl"]["rows"][0]["debit"] = "40"
    support["credit_gl"]["rows"][1]["credit"] = "40"
    intent = build_intent("tenant", "case", source, review, evidence, support)
    assert intent and intent["tax_only"] is True
    assert intent["expected_after"]["subtotal"] == "0"
    assert intent["expected_after"]["taxTotal"] == "40"
    assert intent["proposed_fields"]["taxTotal"] == 40.0
    assert "taxRate" not in intent["proposed_fields"]
    assert intent["required_transport"] == "native_accounting_amendment_with_tax_preview"
    assert intent["financial_write_authorized"] is False


def test_tax_only_line_matching_preserves_kit_quantity_and_rejects_ambiguous_skus():
    from app.services.transaction_ops.source_line_alignment import _tax_only_changes

    source, _, evidence, _ = fixture()
    order = evidence["sections"]["sales_order"]
    line = order["line_evidence"]["lines"][0]
    # Existing kit economics agree; the source has one kit, ERP has two units.
    line.update(
        custcol_fw_solidus_line_id=None,
        custcol_fw_original_ecom_sku="MEM64",
        lineUniqueKey="120",
        quantity="2",
        rate="600",
        amount="1200",
    )
    mapped = _tax_only_changes(source, order)
    assert mapped is not None
    changes, identities = mapped
    assert changes == [{"line": line["line"], "custcol_fw_vat_amount": "120"}]
    assert identities[0]["native_quantity_preserved"] == "2"
    assert identities[0]["native_rate_preserved"] == "600"
    assert "rate" not in changes[0] and "quantity" not in changes[0]
    order["line_evidence"]["lines"].append(deepcopy(line))
    assert _tax_only_changes(source, order) is None


def test_tax_only_header_proof_never_grants_item_write_authority():
    from app.services.transaction_ops.line_evidence import source_tax_refund_delta

    source, _, evidence, _ = fixture()
    source.update(total="1720", item_total="1600", payment_total="1720")
    source["line_items"][0]["price"] = "1600"
    evidence["sections"]["sales_order"]["line_evidence"]["lines"][0]["custcol_fw_solidus_line_id"] = None
    proof = source_tax_refund_delta(source, evidence, "10")
    assert proof and proof["tax_delta"] == "-40"
    assert proof["item_line_amendment_authorized"] is False
    source["total"] = "1720.01"
    assert source_tax_refund_delta(source, evidence, "10") is None


def test_zero_ledger_row_without_account_does_not_hide_a_valid_tax_allocation():
    source, review, evidence, support = fixture()
    support["invoice_gl"]["rows"].append({"accountingbook": "1", "debit": "0", "credit": "0"})
    assert build_intent("tenant", "case", source, review, evidence, support)
    support["invoice_gl"]["rows"][-1]["debit"] = "0.01"
    assert build_intent("tenant", "case", source, review, evidence, support) is None
