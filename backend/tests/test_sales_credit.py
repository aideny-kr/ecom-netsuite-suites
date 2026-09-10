from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.transaction_ops.sales_credit import build_candidate, duplicate_query, external_id
from tests.test_commercial_credits import fixture

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def inputs(*, paid="101"):
    source, invoice, applications, gl = fixture()
    source.update(
        number="R123456789",
        business_entity="Framework Inc",
        payment_total=paid,
        payment_state="paid" if Decimal(paid) == 101 else "balance_due",
    )
    invoice.update(
        entity={"id": "70"},
        account={"id": "100"},
        createdFrom={"id": "90"},
        subtotal="100",
        shippingCost="0",
        discountTotal="0",
        amountPaid=paid,
        amountRemaining=str(Decimal(106) - Decimal(paid)),
    )
    profile = dict(
        schema_version=1,
        account_id="123",
        subsidiary_id="1",
        currency="USD",
        reference_credit_id="30",
        item_id="50",
        adjustment_account_id="500",
        ar_account_id="100",
        accounting_book_id="1",
        source_adjustment_label="Reseller Adjustment 5%",
    )
    scope = dict(netsuite_account_id="123", subsidiary_id="1", source_connection_id="source", record_type="salesorder")
    review = dict(
        sales_credit_profile=profile,
        scope=scope,
        configuration_status="scoped_configuration_found",
        connection_active=True,
        native_mcp_connector_id="connector",
        netsuite_connection_id="connection",
        config_id="config",
        business_entity_subsidiaries={"Framework Inc": "1"},
    )
    report = dict(
        order_reference=source["number"],
        source=dict(record_id="10", currency="USD", currency_minor_unit=2),
        balance={"amounts": {"refunds": {"source": "0", "target": "0", "delta": "0"}}},
    )
    ref = deepcopy(applications["documents"]["30"])
    ref.update(shippingCost="0", discountTotal="0")
    applications["links"] = [r for r in applications["links"] if r["type"] == "DepAppl"]
    applications["documents"].pop("30")
    if Decimal(paid) == 0:
        applications["links"] = []
        applications["documents"] = {}
    else:
        deposit = applications["documents"]["40"]
        deposit.update(total=paid, applied=paid)
        deposit["applications"][0]["amount"] = paid
        applications["links"][0]["foreignamount"] = paid
    support = dict(
        invoice=invoice,
        linked_documents={"complete": True, "rows": [{"id": "20", "type": "CustInvc"}]},
        order_id="90",
        observed_at=NOW.isoformat(),
        applications=applications,
        invoice_gl=gl,
        reference_credit=ref,
        reference_gl=deepcopy(applications["credit_gl"]["30"]),
        item={"id": "50", "isInactive": False, "account": {"id": "500"}},
        currency={"id": "1", "symbol": "USD"},
        posting_date="2026-09-10",
        period=dict(
            id="171",
            startDate="2026-09-01",
            endDate="2026-09-30",
            closed=False,
            arLocked=False,
            allLocked=False,
            isAdjust=False,
            isYear=False,
            isQuarter=False,
        ),
        refunds={
            side: dict(
                complete=True, amount="0", order_reference=source["number"], currency="USD", observed_at=NOW.isoformat()
            )
            for side in ("source", "target")
        },
        duplicates=dict(rows=[], complete=True, posting_key=external_id("tenant", scope, source, "20")),
    )
    return dict(
        tenant_id="tenant", case_id="case", source=source, report=report, review=review, support=support, now=NOW
    )


def test_exact_missing_credit_candidate_has_one_invoice_application_and_no_cloned_identity():
    data = inputs()
    before = deepcopy(data)
    p = build_candidate(**data)
    assert data == before and p
    f = p["proposed_fields"]
    assert f["item"]["items"] == [{"item": {"id": "50"}, "rate": 5.0, "amount": 5.0, "isTaxable": False}]
    assert f["apply"]["items"] == [{"doc": {"id": "20"}, "apply": True, "amount": 5.0}]
    assert f["autoApply"] is False and f["toBeEmailed"] is False
    assert "createdFrom" not in f and "id" not in f and "tranId" not in f
    assert f["entity"] == {"id": "70"} and p["lock_record_type"] == "invoice"
    assert p["expected_after"]["credit_tax"] == "0.00"
    assert p["record_type"] == "creditmemo" and p["mutation_type"] == "create"


@pytest.mark.parametrize("paid,remaining", [("0", "101.00"), ("25", "76.00"), ("101", "0.00")])
def test_credit_preserves_unpaid_and_partially_paid_receivables(paid, remaining):
    p = build_candidate(**inputs(paid=paid))
    assert p["expected_after"]["invoice_remaining"] == remaining
    assert p["expected_after"]["remaining_variance"] == "0.00"
    assert p["proposed_fields"]["apply"]["items"] == [{"doc": {"id": "20"}, "apply": True, "amount": 5.0}]


@pytest.mark.parametrize("paid", ["-1", "102"])
def test_credit_rejects_negative_payments_and_overpayments(paid):
    assert build_candidate(**inputs(paid=paid)) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["support"]["linked_documents"].update(complete=False),
        lambda d: d["support"]["linked_documents"].update(rows=[]),
        lambda d: d["support"]["linked_documents"]["rows"].append({"id": "21", "type": "CustInvc"}),
        lambda d: d["support"]["linked_documents"]["rows"][0].update(id="21"),
        lambda d: d["support"]["linked_documents"]["rows"][0].update(type="CashSale"),
        lambda d: d["support"]["duplicates"].update(rows=[{"id": "999"}]),
        lambda d: d["support"]["duplicates"].update(complete=False),
        lambda d: d["support"]["duplicates"].update(posting_key="other"),
        lambda d: d["review"]["scope"].update(netsuite_account_id="123-sb1"),
        lambda d: d["review"].update(connection_active=False),
        lambda d: d["review"].update(configuration_status="ambiguous"),
        lambda d: d["source"]["adjustments"][0].update(finalized=False),
        lambda d: d["source"]["adjustments"][0].update(label="Unrelated promotion"),
        lambda d: d["source"].update(payment_total="100.99"),
        lambda d: d["source"].update(payment_state="balance_due"),
        lambda d: d["support"]["invoice"].update(amountRemaining="0"),
        lambda d: d["support"]["invoice"].update(amountRemaining="5.01"),
        lambda d: d["support"]["invoice"].update(amountPaid="100.99"),
        lambda d: d["support"]["invoice"].update(subsidiary={"id": "2"}),
        lambda d: d["support"]["invoice"].update(account={"id": "999"}),
        lambda d: d["support"]["invoice"].update(createdFrom={"id": "999"}),
        lambda d: d["support"]["invoice"].update(exchangeRate="1.01"),
        lambda d: d["support"]["currency"].update(symbol="EUR"),
        lambda d: d["support"]["currency"].update(id="2"),
        lambda d: d["support"]["reference_credit"].update(taxTotal="0.01"),
        lambda d: d["support"]["reference_credit"].update(id="99"),
        lambda d: d["support"]["item"].update(isInactive=True),
        lambda d: d["support"]["item"].update(account={"id": "999"}),
        lambda d: d["support"]["reference_gl"]["rows"][0].update(account="999"),
        lambda d: d["support"]["invoice_gl"]["rows"][0].update(debit="106.01"),
        lambda d: d["support"]["invoice_gl"].update(complete=False),
        lambda d: d["support"]["applications"].update(complete=False),
        lambda d: d["support"]["applications"]["links"].append(deepcopy(d["support"]["applications"]["links"][0])),
        lambda d: d["support"]["applications"]["documents"]["40"]["applications"][0].update(doc={"id": "999"}),
        lambda d: d["support"]["refunds"]["source"].update(amount="0.01"),
        lambda d: d["support"]["refunds"]["target"].update(complete=False),
        lambda d: d["support"]["refunds"]["source"].update(observed_at="2026-09-10T11:00:00Z"),
        lambda d: d["support"]["period"].update(closed=True),
        lambda d: d["support"]["period"].update(arLocked=True),
        lambda d: d["support"]["period"].update(isAdjust=True),
        lambda d: d["support"].update(posting_date="2026-10-01"),
        lambda d: d["support"].update(observed_at="2026-09-10T11:00:00Z"),
    ],
)
def test_incomplete_changed_or_duplicate_evidence_never_authorizes_candidate(mutate):
    data = inputs()
    mutate(data)
    assert build_candidate(**data) is None


def test_idempotency_key_is_stable_across_amount_and_period_retries_but_scoped_to_tenant_invoice():
    d = inputs()
    a = external_id("tenant", d["review"]["scope"], d["source"], "20")
    d["source"]["adjustments"][0]["amount"] = "-6"
    assert a == external_id("tenant", d["review"]["scope"], d["source"], "20")
    assert a != external_id("other-tenant", d["review"]["scope"], d["source"], "20")
    assert a != external_id("tenant", d["review"]["scope"], d["source"], "21")


def test_duplicate_search_does_not_hide_old_partial_or_unapplied_credits():
    d = inputs()
    q = duplicate_query(
        d["support"]["invoice"],
        d["review"]["sales_credit_profile"],
        d["source"]["number"],
        d["support"]["duplicates"]["posting_key"],
    )
    assert "SELECT DISTINCT" in q and "t.type='CustCred'" in q and "t.entity=70" in q
    assert "tl.item=50" in q and "t.externalid=" in q and "tl.createdfrom=20" in q
    assert not any(x in q.lower() for x in ("trandate", "amountremaining", "status", "foreigntotal", "sum("))
    d["support"]["invoice"]["entity"]["id"] = "7 OR 1=1"
    with pytest.raises(ValueError):
        duplicate_query(
            d["support"]["invoice"],
            d["review"]["sales_credit_profile"],
            d["source"]["number"],
            d["support"]["duplicates"]["posting_key"],
        )
