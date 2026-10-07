"""Smart resolver, slice 1: a credit memo the agent proposes, accepted only by outcome.

The agent chooses the items and amounts. The server accepts them only when the order's posted
balance (invoices less credits, tax classified by GL account) then equals the finalized source in
gross, net and tax to the cent. No per-label or per-cause code: R231821517 is just one shape.
"""

from copy import deepcopy
from decimal import Decimal

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


# --- reads (fake NetSuite) ---------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID  # noqa: E402

CASE = UUID("e9c89ea2-e45e-45d0-8dda-e6ca677620f4")
SCOPE = {
    "netsuite_account_id": "6738075",
    "subsidiary_id": "1",
    "source_connection_id": "s",
    "record_type": "salesorder",
}


def _netsuite(*, credits=(), named=(), graph_credits=()):
    """A NetSuite with order R231821517, invoice 16029044 and the given credits."""
    invoice = {
        "id": "16029044",
        "tranId": "INV371382",
        "total": Decimal("13494.75"),
        "amountRemaining": Decimal("13494.75"),
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "exchangeRate": Decimal("1.0"),
        "location": {"id": "30"},
        "department": {"id": "18"},
        "createdFrom": {"id": "15945327"},
    }
    gl = [
        {"transaction": 16029044, "account": 119, "accountingbook": 1, "debit": Decimal("13494.75"), "credit": None},
        {"transaction": 16029044, "account": 54, "accountingbook": 1, "debit": None, "credit": Decimal("13494.75")},
    ]
    docs = {}
    for c in credits:
        docs[c["id"]] = c
        gl += [
            {"transaction": int(c["id"]), "account": 119, "accountingbook": 1, "debit": None, "credit": c["total"]},
            {"transaction": int(c["id"]), "account": 774, "accountingbook": 1, "debit": c["total"], "credit": None},
        ]
    calls = []

    class Reader:
        async def request(self, method, path, *, params=None, body=None):
            calls.append((method, path, (body or {}).get("q")))
            q = (body or {}).get("q") or ""
            if path == "/query/v1/suiteql":
                if "t.type = 'SalesOrd'" in q:
                    rows = [{"id": 15945327, "tranid": "R231821517"}]
                elif "IN ('CustInvc', 'CashSale')" in q:
                    rows = [{"id": 16029044}]
                elif "nexttransactionlinelink" in q:
                    rows = [{"id": int(c["id"])} for c in credits]
                elif "tl.createdfrom = 16029044" in q:
                    rows = []
                elif "t.memo LIKE" in q:
                    rows = [{"id": int(i)} for i in named]
                elif "type = 'CustCred' AND id IN" in q:
                    rows = [{"id": int(i)} for i in graph_credits]
                elif "transactionaccountingline" in q:
                    ids = q.split("IN (")[1].split(")")[0].split(",")
                    rows = [r for r in gl if str(r["transaction"]) in ids]
                elif "FROM item" in q:
                    rows = [
                        {"id": 1471, "isinactive": "F", "incomeaccount": 774, "itemtype": "Discount"},
                        {"id": 5005, "isinactive": "F", "incomeaccount": 210, "itemtype": "NonInvtPart"},
                    ]
                elif "FROM account " in q:
                    rows = [
                        {"id": 119, "accttype": "AcctRec"},
                        {"id": 54, "accttype": "Income"},
                        {"id": 774, "accttype": "Income"},
                    ]
                elif "FROM accountingperiod" in q:
                    rows = [{"id": 173}]
                else:
                    raise AssertionError(q)
                return {"items": rows, "count": len(rows), "totalResults": len(rows), "hasMore": False}
            if path == "/record/v1/invoice/16029044":
                return invoice
            if path == "/record/v1/salesOrder/15945327":
                return {
                    "id": "15945327",
                    "tranId": "R231821517",
                    "total": Decimal("13494.75"),
                    "entity": {"id": "5658593"},
                    "subsidiary": {"id": "1"},
                    "currency": {"id": "1"},
                    "lastModifiedDate": "2026-09-30T13:35:00Z",
                    "item": {"items": [{"line": 1, "item": {"id": "70"}, "amount": Decimal("13494.75")}]},
                }
            if path.startswith("/record/v1/creditMemo/"):
                return docs[path.rsplit("/", 1)[1]]
            if path.startswith("/record/v1/accountingPeriod/"):
                return {"id": "173", "closed": False, "arLocked": False, "allLocked": False}
            if path.startswith("/record/v1/currency/"):
                return {"id": "1", "symbol": "USD", "currencyPrecision": 2}
            if path == "/record/v1/metadata-catalog/creditMemo":
                return {"properties": {}}
            raise AssertionError(path)

    return Reader(), calls


def _patch_reads(monkeypatch, reader, graph_ids=()):
    case = SimpleNamespace(id=CASE, order_reference="R231821517", scope_json=SCOPE, latest_report_json={})
    review = {
        "configuration_status": "scoped_configuration_found",
        "connection_active": True,
        "native_mcp_connector_id": "conn",
        "scope": SCOPE,
        "config_id": "fd06b784-9d74-42e8-aa84-d8885fc58005",
        "netsuite_connection_id": "3871205d-2a4c-4c56-a069-285c5f74836e",
        "business_entity_subsidiaries": {"Framework Inc": "1", "Framework BV": "2"},
    }
    config = SimpleNamespace(
        mapping_json={
            "refund_adjustments": {
                "schema_version": 1,
                "account_id": "6738075",
                "subsidiary_id": "1",
                "tax_reversal_reason_ids": ["4"],
                "tax_item_accounts": {"5005": "210"},
                "tax_accounts": ["210"],
            }
        }
    )

    async def get_case(db, tenant_id, case_id):
        return case

    async def context(db, tenant_id, scope, report):
        return review

    async def source(db, tenant_id, scope, ref, **kw):
        return {**_facts()["source"], "business_entity": "Framework Inc"}

    async def refunds(reader, order_id, subsidiary, currency, *, order_reference):
        return {"dependency_manifest": {"truncated": False, "transaction_ids": list(graph_ids)}}

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        yield reader

    monkeypatch.setattr("app.services.transaction_ops.case_service.get_case", get_case)
    monkeypatch.setattr("app.services.transaction_ops.accounting_review.accounting_context", context)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.refresh_source", source)
    monkeypatch.setattr("app.services.transaction_ops.netsuite_refunds.collect_refunds", refunds)
    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", authenticated)

    async def scalar(*a, **k):
        return config

    return SimpleNamespace(scalar=scalar, info={})


def _credit(ident, total, *, applied_to="16029044", unapplied="0", memo="R231821517 other", ext=None):
    total, unapplied = Decimal(str(total)), Decimal(unapplied)
    return {
        "id": ident,
        "tranId": f"CM{ident}",
        "total": total,
        "applied": total - unapplied,
        "unapplied": unapplied,
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "exchangeRate": Decimal("1.0"),
        "memo": memo,
        "externalId": ext,
        "item": {"items": [{"line": 1, "item": {"id": "1471"}, "amount": total, "quantity": 1}]},
        "apply": {"items": [{"doc": {"id": applied_to}, "apply": True, "amount": total - unapplied}]},
    }


@pytest.mark.asyncio
async def test_gather_reads_the_order_invoice_and_gl_and_the_outcome_check_accepts_the_credit(monkeypatch):
    reader, calls = _netsuite()
    db = _patch_reads(monkeypatch, reader)
    found, context = await cc.gather(db, "tenant", CASE, ["1471"])
    assert [d["id"] for d, _ in found["invoices"]] == ["16029044"] and found["credits"] == []
    assert found["period"]["id"] == "173" and found["invoices"][0][0]["currency_code"] == "USD"
    assert (context["order"]["id"], context["order"]["tranId"]) == ("15945327", "R231821517")
    assert context["order"]["lines"] and context["order"]["lastModifiedDate"]
    result = cc.assess(lines=LINES, memo="reseller discount", **found)
    assert result["balance"]["after"]["gross"] == "12820.02"


@pytest.mark.asyncio
async def test_a_credit_applied_to_the_invoice_counts_so_no_second_credit_is_proposed(monkeypatch):
    reader, _ = _netsuite(credits=[_credit("16123312", 674.73)])
    db = _patch_reads(monkeypatch, reader)
    found, _ = await cc.gather(db, "tenant", CASE, ["1471"])
    assert [d["id"] for d, _ in found["credits"]] == ["16123312"]
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=LINES, memo="x", **found)
    assert exc.value.code == "no_difference"


@pytest.mark.asyncio
async def test_a_credit_naming_the_order_but_not_applied_to_its_invoice_stops_the_proposal(monkeypatch):
    stray = _credit("16200000", 674.73, applied_to="999", memo="R231821517 standalone")
    reader, _ = _netsuite(credits=[stray], named=["16200000"])
    # The fake also lists it as applied; mark it applied elsewhere through its own apply sublist.
    db = _patch_reads(monkeypatch, reader)
    with pytest.raises(cc.RefusalError) as exc:
        await cc.gather(db, "tenant", CASE, ["1471"])
    assert exc.value.code == "existing_credit_not_applied_to_invoice"


# --- readback ---------------------------------------------------------------------------------


def _approved_proposal():
    found = _facts()
    result = cc.assess(lines=LINES, memo="reseller discount", **found)
    ext = "ss-credit-abc"
    return {
        "kind": cc.KIND,
        "case_id": str(CASE),
        "lines": LINES,
        "memo": result["proposed_fields"]["memo"],
        "proposed_fields": {**result["proposed_fields"], "externalId": ext},
        "expected_after": result["expected_after"],
        "expected_ledger": result["expected_ledger"],
        "source": found["source"],
        "invoice_id": "16029044",
        "sales_order_id": "15945327",
        "support": {"baseline": cc.baseline(found, {"order": {"id": "15945327"}})},
    }


def _after_post(p, **over):
    found = _facts()
    credit = {
        "id": "16123312",
        "tranId": "CM12127",
        "total": "674.73",
        "applied": "674.73",
        "unapplied": "0",
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "exchangeRate": "1.0",
        "memo": p["proposed_fields"]["memo"],
        "externalId": p["proposed_fields"]["externalId"],
        "line_evidence": {"complete": True, "lines": [{"item": {"id": "1471"}, "amount": "674.73"}]},
    }
    credit.update(over.pop("credit", {}))
    gl = {
        "complete": True,
        "rows": [
            {"account": "119", "accountingbook": "1", "credit": "674.73"},
            {"account": "774", "accountingbook": "1", "debit": over.pop("debit", "674.73")},
        ],
    }
    found["credits"] = [(credit, gl)]
    found["invoices"][0][0]["amountRemaining"] = "12820.02"
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change, outcome",
    [
        ({}, "verified"),
        ({"credit": {"externalId": "other"}}, "credit_creation_not_found:0"),
        ({"credit": {"memo": "R231821517 something else"}}, "credit_creation_memo_differs"),
        ({"credit": {"unapplied": "1.00", "applied": "673.73"}}, "credit_creation_not_fully_applied"),
        (
            {"credit": {"line_evidence": {"complete": True, "lines": [{"item": {"id": "5005"}, "amount": "674.73"}]}}},
            "credit_creation_lines_differ",
        ),
    ],
)
async def test_readback_requires_the_approved_credit_and_an_order_that_now_agrees(monkeypatch, change, outcome):
    p = _approved_proposal()
    found = _after_post(p, **deepcopy(change))

    async def fresh(db, tenant_id, proposal):
        return found, {"order": {"id": "15945327"}}

    monkeypatch.setattr(cc, "fresh", fresh)
    result = await cc.verify_after(SimpleNamespace(info={}), "tenant", p, {"recordId": "16123312"})
    if outcome == "verified":
        assert result["status"] == "verified" and result["credit_memo_number"] == "CM12127"
        assert result["balance"]["booked"] == result["balance"]["source"]
    else:
        assert (result["status"], result["reason"]) == ("needs_review", outcome)


# --- the agent's tool and playbook ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_returns_only_the_exact_card_call_never_amounts_to_restate(monkeypatch):
    from app.mcp.tools import transaction_ops_tools as tools

    db = SimpleNamespace(info={})
    actor = SimpleNamespace(id="actor")

    async def authorize(context, create):
        return db, "tenant", actor

    async def no_scope(*a, **k):
        return None

    async def log(*a, **k):
        return SimpleNamespace(id="audit-1")

    async def propose(db_, tenant_id, case_id, lines, memo, reason):
        p = {
            "connector_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "record_type": "creditmemo",
            "wire_record_json": '{"memo": "R231821517 reseller discount"}',
            "case_id": str(case_id),
        }
        db_.info["accounting_correction_candidate"] = p
        return p

    monkeypatch.setattr(tools, "_authorize", authorize)
    monkeypatch.setattr("app.services.transaction_ops.case_resolution_scope.load", no_scope)
    monkeypatch.setattr("app.services.audit_service.log_event", log)
    monkeypatch.setattr(cc, "propose", propose)
    out = await tools.execute_propose_credit({"case_id": str(CASE), "lines": LINES, "memo": "reseller discount"})
    assert out["success"] is True and out["financial_writes"] == 0
    call = out["correction_candidate"]
    assert call["tool_name"] == "ext__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa__ns_createRecord"
    assert call["params"] == {"recordType": "creditMemo", "data": '{"memo": "R231821517 reseller discount"}'}
    assert "674.73" not in str(out)


@pytest.mark.asyncio
async def test_a_refusal_returns_its_code_figures_and_guidance(monkeypatch):
    from app.mcp.tools import transaction_ops_tools as tools

    db = SimpleNamespace(info={})

    async def authorize(context, create):
        return db, "tenant", SimpleNamespace(id="actor")

    async def no_scope(*a, **k):
        return None

    async def log(*a, **k):
        return SimpleNamespace(id="audit-1")

    async def propose(*a, **k):
        raise cc.RefusalError("no_difference", {"booked": {"gross": "1.00"}})

    monkeypatch.setattr(tools, "_authorize", authorize)
    monkeypatch.setattr("app.services.transaction_ops.case_resolution_scope.load", no_scope)
    monkeypatch.setattr("app.services.audit_service.log_event", log)
    monkeypatch.setattr(cc, "propose", propose)
    out = await tools.execute_propose_credit({"case_id": str(CASE), "lines": LINES})
    assert (out["success"], out["refused"]) == (False, "no_difference")
    assert "NetSuite is right" in out["guidance"]
    bad = await tools.execute_propose_credit({"case_id": str(CASE), "lines": LINES, "record_id": "1"})
    assert bad["success"] is False


def test_the_accounting_playbook_routes_an_uncovered_over_posting_to_the_new_credit():
    from app.services.chat.tools import build_local_tool_definitions
    from app.services.chat.skills import get_skill_instructions

    core = get_skill_instructions("accounting_operations")
    assert "credit_creation" in core and "transaction_ops_propose_credit" in core
    method = get_skill_instructions("credit_creation")
    for rule in ("never round", "Never create a second credit", "exact params", "configured tax-refund item"):
        assert rule.lower() in method.lower()
    assert "transaction_ops_propose_credit" in {t["name"] for t in build_local_tool_definitions()}


# --- review round 1 -----------------------------------------------------------------------------


def test_r1_f1_the_server_card_creates_a_new_credit_and_updates_everything_else():
    """F1: the server card decided create-vs-update by listing kinds, so a credit_creation
    proposal became an update and its card never displayed. The proposal's own mutation_type
    decides now."""
    from app.services.transaction_ops.tax_correction import card_call

    base = {
        "connector_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "record_type": "creditmemo",
        "record_id": "20",
        "execution_transport": "mcp_record_api",
        "wire_record_json": "{}",
    }
    mutation, name, params = card_call({**base, "kind": cc.KIND, "mutation_type": "create"})
    assert (mutation, name.endswith("__ns_createRecord"), "recordId" in params) == ("create", True, False)
    mutation, name, params = card_call({**base, "kind": "credit_line_reallocation"})
    assert (mutation, name.endswith("__ns_updateRecord"), params["recordId"]) == ("update", True, "20")
    mutation, name, _ = card_call(
        {
            **base,
            "kind": "sales_adjustment_credit",
            "mutation_type": "create",
            "execution_transport": None,
            "proposed_fields": {},
        }
    )
    assert (mutation, name.endswith("__ns_createRecord")) == ("create", True)


def test_r1_f2_the_proposal_keeps_the_invoice_to_order_edge_the_recheck_binds_through(monkeypatch):
    """F2: the invoice projection dropped createdFrom, so the recheck could not bind the report."""
    from app.services.transaction_ops import credit_api_correction
    from app.services.transaction_ops.treatments import reconciliation_target_id

    monkeypatch.setattr(credit_api_correction, "schema_contract", lambda raw, fields: {"digest": "x"})
    monkeypatch.setattr(credit_api_correction, "typed_fields", lambda raw, fields: fields)
    found = _facts()
    found["invoices"][0][0]["createdFrom"] = {"id": "15945327"}
    result = cc.assess(lines=LINES, memo="reseller discount", **found)
    context = {
        "review": {
            "scope": SCOPE,
            "config_id": "c",
            "netsuite_connection_id": "n",
            "native_mcp_connector_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
        "case_id": str(CASE),
        "order": {"id": "15945327"},
        "catalog": {},
        "refund_graph": {},
    }
    p = cc._proposal("tenant", found, context, result, LINES, "reseller discount", "r")
    assert p["mutation_type"] == "create"
    assert p["support"]["invoice"]["createdFrom"] == {"id": "15945327"}
    assert reconciliation_target_id(p) == "15945327"


@pytest.mark.asyncio
async def test_r1_f2_gather_keeps_created_from_on_the_invoice(monkeypatch):
    reader, _ = _netsuite()
    reader_invoice = {"createdFrom": {"id": "15945327", "refName": "Sales Order #R231821517"}}
    orig = reader.request

    async def request(method, path, **kw):
        out = await orig(method, path, **kw)
        return {**out, **reader_invoice} if path == "/record/v1/invoice/16029044" else out

    reader.request = request
    db = _patch_reads(monkeypatch, reader)
    found, _ = await cc.gather(db, "tenant", CASE, ["1471"])
    assert found["invoices"][0][0]["createdFrom"]["id"] == "15945327"


def test_r1_f3_a_line_posting_to_a_liability_or_unknown_account_is_refused_before_any_write():
    facts = _facts()
    facts["items"]["999"] = {"id": "999", "isInactive": False, "itemType": "OthCharge", "incomeAccount": {"id": "870"}}
    facts["account_types"]["870"] = "OthCurrLiab"
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=[{"item_id": "999", "amount": "674.73"}], memo="x", **facts)
    assert exc.value.code == "account_not_supported"
    facts["items"]["998"] = {"id": "998", "isInactive": False, "itemType": "OthCharge", "incomeAccount": {"id": "871"}}
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=[{"item_id": "998", "amount": "674.73"}], memo="x", **facts)
    assert exc.value.code == "account_not_supported"  # unknown type: never assumed net


@pytest.mark.asyncio
async def test_r1_f3_gather_reads_the_type_of_every_proposed_items_account(monkeypatch):
    reader, calls = _netsuite()
    db = _patch_reads(monkeypatch, reader)
    await cc.gather(db, "tenant", CASE, ["1471"])
    (query,) = [q for _, path, q in calls if q and "FROM account " in q]
    assert "774" in query.split("IN (")[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["invoice", "order", "existing_credit"])
async def test_r1_f4_readback_requires_the_approved_invoice_order_and_unchanged_existing_credits(monkeypatch, change):
    p = _approved_proposal()
    found = _after_post(p)
    p["sales_order_id"] = "15945327"
    p["support"] = {"baseline": cc.baseline(_facts(), {"order": {"id": "15945327"}})}
    context = {"order": {"id": "15945327"}}
    if change == "invoice":
        found["invoices"][0][0]["id"] = "999999"
    elif change == "order":
        context = {"order": {"id": "1"}}
    else:
        older = deepcopy(found["credits"][0])
        older[0].update(id="15", externalId=None, total="1.00")
        found["credits"].append(older)

    async def fresh(db, tenant_id, proposal):
        return found, context

    monkeypatch.setattr(cc, "fresh", fresh)
    result = await cc.verify_after(SimpleNamespace(info={}), "tenant", p, {"recordId": "16123312"})
    assert result["status"] == "needs_review"
    assert result["reason"].startswith("credit_creation_related_record_changed")


# --- review round 2 -----------------------------------------------------------------------------


def _existing_credit(ident, total="1.00", debit_account="774"):
    doc = {
        "id": ident,
        "total": total,
        "applied": total,
        "unapplied": "0",
        "externalId": None,
        "account": {"id": "119"},
        "entity": {"id": "5658593"},
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "exchangeRate": "1.0",
    }
    gl = {
        "complete": True,
        "rows": [
            {"account": "119", "accountingbook": "1", "credit": total},
            {"account": debit_account, "accountingbook": "1", "debit": total},
        ],
    }
    return doc, gl


def test_r2_f1_the_proposal_leaves_room_for_its_own_credit_within_the_readback_bound():
    """F1: eight existing credits passed the proposal, then nine could never be read back."""
    facts = _facts()
    facts["credits"] = [_existing_credit(str(100 + i)) for i in range(cc.MAX_CREDITS)]
    with pytest.raises(cc.RefusalError) as exc:
        cc.assess(lines=[{"item_id": "1471", "amount": "666.73"}], memo="x", **facts)
    assert exc.value.code == "too_many_credits"
    facts["credits"] = facts["credits"][: cc.MAX_CREDITS - 1]
    assert cc.assess(lines=[{"item_id": "1471", "amount": "667.73"}], memo="x", **facts)


@pytest.mark.asyncio
async def test_r2_f2_the_source_order_must_belong_to_the_cases_subsidiary(monkeypatch):
    """F2: a matching reference in another legal entity must never be credited."""
    reader, _ = _netsuite()
    db = _patch_reads(monkeypatch, reader)

    async def other_entity(db, tenant_id, scope, ref, **kw):
        return {**_facts()["source"], "business_entity": "Framework BV"}

    monkeypatch.setattr("app.services.transaction_ops.tax_correction.refresh_source", other_entity)
    with pytest.raises(cc.RefusalError) as exc:
        await cc.gather(db, "tenant", CASE, ["1471"])
    assert exc.value.code == "source_scope_mismatch"


@pytest.mark.asyncio
async def test_r2_f3_readback_detects_an_existing_credit_whose_accounts_changed(monkeypatch):
    p = _approved_proposal()
    before = _facts()
    before["credits"] = [_existing_credit("15", "1.00", "774")]
    p["support"] = {"baseline": cc.baseline(before, {"order": {"id": "15945327"}})}
    found = _after_post(p)
    found["credits"].append(_existing_credit("15", "1.00", "54"))  # same amount, other account

    async def fresh(db, tenant_id, proposal):
        return found, {"order": {"id": "15945327"}}

    monkeypatch.setattr(cc, "fresh", fresh)
    result = await cc.verify_after(SimpleNamespace(info={}), "tenant", p, {"recordId": "16123312"})
    assert result["status"] == "needs_review" and result["reason"] == "credit_creation_related_record_changed"


def test_r2_f4_a_truncated_memo_is_stable_across_reassessment():
    memo = "x" * 488 + " remainder"
    first = cc.assess(lines=LINES, memo=memo, **_facts())["proposed_fields"]["memo"]
    second = cc.assess(lines=LINES, memo=first, **_facts())["proposed_fields"]["memo"]
    assert first == second and len(first) <= cc.MEMO_MAX and first == first.strip()


# --- review round 3: the evidence approval and readback compare is complete and canonical -------


@pytest.mark.asyncio
async def test_r3_f1_the_protected_sales_order_is_the_whole_record(monkeypatch):
    reader, calls = _netsuite()
    db = _patch_reads(monkeypatch, reader)
    found, context = await cc.gather(db, "tenant", CASE, ["1471"])
    order = context["order"]
    assert (
        order["id"] == "15945327"
        and order["total"] == Decimal("13494.75")
        and order["lastModifiedDate"] == "2026-09-30T13:35:00Z"
    )
    changed = {**context, "order": {**order, "total": Decimal("1.00")}}
    assert cc._identity(found, context) != cc._identity(found, changed)
    assert cc.baseline(found, context) != cc.baseline(found, changed)


@pytest.mark.asyncio
async def test_r3_f2_the_credit_search_covers_every_customer_in_the_subsidiary(monkeypatch):
    reader, calls = _netsuite()
    db = _patch_reads(monkeypatch, reader)
    await cc.gather(db, "tenant", CASE, ["1471"])
    (query,) = [q for _, _, q in calls if q and "t.memo LIKE" in q]
    assert "t.entity" not in query and "subsidiary = 1" in query


def test_r3_f3_fingerprints_do_not_depend_on_gl_row_order():
    found = _facts()
    reversed_ = deepcopy(found)
    reversed_["invoices"][0][1]["rows"].reverse()
    context = {"order": {"id": "15945327"}, "refund_graph": {}}
    assert cc.baseline(found, context) == cc.baseline(reversed_, context)
    assert cc._identity(found, context) == cc._identity(reversed_, context)
