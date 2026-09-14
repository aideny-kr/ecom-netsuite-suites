import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat.write_payload import normalize_write_payload
from app.services.transaction_ops import sales_order_alignment as alignment
from app.services.transaction_ops.commercial_credits import source_adjustment_basis
from app.services.transaction_ops.invoice_discount import verified_existing_discount
from tests.test_invoice_discount import unpaid_inputs


def inputs():
    d = unpaid_inputs()
    s = d["support"]
    invoice = deepcopy(s["invoice"])
    invoice.update(total="95", amountRemaining="95", discountTotal="-5", discountRate="-5", discountItem={"id": "50"})
    invoice["item"] = {"items": [{**s["invoice_lines"][0], "orderLine": "10"}], "totalResults": 1}
    order = deepcopy(invoice)
    order.update(
        id="90",
        tranId=d["source"]["number"],
        status={"id": "G"},
        total="100",
        discountTotal="0",
        discountItem=None,
        discountRate=None,
        billingSchedule=None,
        lastModifiedDate="2026-09-13T00:00:00Z",
    )
    order.pop("createdFrom")
    order["item"] = {
        "items": [
            {
                **s["invoice_lines"][0],
                "line": "10",
                "quantityBilled": "1",
                "quantityFulfilled": "1",
                "quantityBackOrdered": "0",
                "isClosed": False,
            }
        ]
    }
    gl = {
        "complete": True,
        "rows": [
            {"account": "100", "accountingbook": "1", "debit": "95"},
            {"account": "200", "accountingbook": "1", "credit": "100"},
            {"account": "500", "accountingbook": "1", "debit": "5"},
        ],
    }
    proof = verified_existing_discount(source_adjustment_basis(d["source"]), invoice, s["applications"], gl, s["item"])
    assert proof
    support = {
        "order": alignment.snapshot(order, amendable=True),
        "invoice": alignment.snapshot(invoice),
        "linked_documents": [{"id": "20", "type": "CustInvc", "foreigntotal": "95"}],
        "invoice_amount_paid": "0",
        "invoice_amount_remaining": "95",
        "invoice_gl": gl,
        "invoice_applications": s["applications"],
        "discount_item": s["item"],
        "currency": {"id": "1", "symbol": "USD", "currencyPrecision": 2},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "tenant_id": d["tenant_id"],
        "case_id": d["case_id"],
        "source": d["source"],
        "review": d["review"],
        "evidence": {"commercial_credit_resolution": proof},
        "support": support,
    }


def test_billed_order_amendment_is_distinct_from_invoice_posting():
    d = inputs()
    before = deepcopy(d)
    p = alignment.build_candidate(**d)
    assert p["record_id"] == "90" and p["invoice_id"] == "20"
    assert p["proposed_fields"] == {"discountItem": {"id": "50"}, "discountRate": -5.0}
    assert p["expected_after"]["total"] == "95"
    assert p["lock_record_type"] == "invoice"
    assert d == before


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("order", "status", {"id": "F"}),
        ("order", "discountItem", {"id": "50"}),
        ("order", "discountRate", "-1"),
        ("order", "total", "101"),
        ("order", "taxTotal", "0.01"),
        ("order", "shippingCost", "0.01"),
        ("order", "billingSchedule", {"id": "1"}),
        ("invoice", "createdFrom", {"id": "99"}),
        ("invoice", "entity", {"id": "99"}),
        ("invoice", "total", "94.99"),
        ("invoice", "status", {"id": "PaidInFull"}),
        ("invoice", "discountItem", {"id": "99"}),
        ("invoice", "exchangeRate", "2"),
        ("currency", "symbol", "CAD"),
        ("currency", "currencyPrecision", 0),
    ],
)
def test_ambiguous_or_changed_native_scope_cannot_propose(section, key, value):
    d = inputs()
    d["support"][section][key] = value
    assert alignment.build_candidate(**d) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("quantityBilled", "0"),
        ("quantityFulfilled", "0"),
        ("quantityBackOrdered", "1"),
        ("isClosed", True),
        ("amount", "99"),
        ("item", {"id": "99"}),
    ],
)
def test_partial_or_different_lines_are_not_amended(key, value):
    d = inputs()
    d["support"]["order"]["lines"][0][key] = value
    assert alignment.build_candidate(**d) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["source"].update(payment_total="1"),
        lambda d: d["source"]["adjustments"][0].update(finalized=False),
        lambda d: d["review"]["sales_credit_profile"].update(source_adjustment_label="Another adjustment"),
        lambda d: d["review"].update(connection_active=False),
        lambda d: d["review"]["business_entity_subsidiaries"].clear(),
        lambda d: d["support"]["linked_documents"].append({"id": "21", "type": "CustInvc"}),
        lambda d: d["support"]["linked_documents"].append({"id": "21", "type": "CustCred"}),
        lambda d: d["support"].update(invoice_amount_paid="1"),
    ],
)
def test_new_evidence_requires_another_treatment(mutation):
    d = inputs()
    mutation(d)
    assert alignment.build_candidate(**d) is None


def test_expanded_sublist_metadata_cannot_hide_incomplete_lines():
    raw = {"item": {"items": [{"line": 1}], "totalResults": 2}}
    with pytest.raises(ValueError, match="Complete native"):
        alignment.snapshot(raw)
    raw["item"]["totalResults"] = 1
    assert alignment.snapshot(raw)["lines"] == [{"line": 1}]


def test_projected_snapshots_exclude_pii_but_detect_unexpected_changes():
    raw = {
        "id": "90",
        "entity": {"id": "70", "refName": "PRIVATE CUSTOMER"},
        "email": "PRIVATE EMAIL",
        "shippingAddress": {"addr1": "PRIVATE ADDRESS"},
        "custbody_taxid": "PRIVATE TAXID",
        "item": {"items": [{"line": 1, "memo": "PRIVATE LINE"}]},
        "total": "100",
        "lastModifiedDate": "before",
    }
    before = alignment.snapshot(raw, amendable=True)
    assert "PRIVATE" not in json.dumps(before)
    amended = deepcopy(raw)
    amended.update(
        total="95", discountRate="-5", discountItem={"id": "50"}, discountTotal="-5", lastModifiedDate="after"
    )
    assert alignment.snapshot(amended, amendable=True)["native_digest"] == before["native_digest"]
    amended["email"] = "DIFFERENT PRIVATE EMAIL"
    assert alignment.snapshot(amended, amendable=True)["native_digest"] != before["native_digest"]
    assert alignment.snapshot(raw)["native_digest"] != alignment.snapshot({**raw, "total": "95"})["native_digest"]


def test_proposal_payload_and_native_line_count_are_bounded():
    d = inputs()
    p = alignment.build_candidate(**d)
    assert "order" not in p["support"]
    assert len(json.dumps(p, separators=(",", ":")).encode()) < alignment.MAX_PROPOSAL_BYTES
    d["source"]["oversized"] = "x" * alignment.MAX_PROPOSAL_BYTES
    assert alignment.build_candidate(**d) is None
    with pytest.raises(ValueError, match="review limit"):
        alignment.snapshot({"item": {"items": [{"line": n} for n in range(alignment.MAX_LINES + 1)]}})


@pytest.mark.parametrize("amendable", [False, True])
def test_live_inventory_availability_does_not_stale_transaction_evidence(amendable):
    raw = {"id": "90", "item": {"items": [{"line": 1, "quantity": "2", "quantityAvailable": "100"}]}}
    original = deepcopy(raw)
    before = alignment.snapshot(raw, amendable=amendable)
    changed = deepcopy(raw)
    changed["item"]["items"][0]["quantityAvailable"] = "37"
    assert alignment.snapshot(changed, amendable=amendable) == before
    del changed["item"]["items"][0]["quantityAvailable"]
    assert alignment.snapshot(changed, amendable=amendable) == before
    assert raw == original


@pytest.mark.parametrize("amendable", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "quantity",
        "quantityBilled",
        "quantityFulfilled",
        "quantityBackOrdered",
        "amount",
        "rate",
        "taxAmount",
        "location",
        "custcol_example",
    ],
)
def test_availability_exclusion_still_detects_transaction_line_changes(amendable, field):
    raw = {"id": "90", "item": {"items": [{"line": 1, "quantityAvailable": "100", field: "2"}]}}
    before = alignment.snapshot(raw, amendable=amendable)
    raw["item"]["items"][0][field] = "3"
    assert alignment.snapshot(raw, amendable=amendable)["native_digest"] != before["native_digest"]


def test_availability_exclusion_is_limited_to_expanded_item_lines():
    raw = {"quantityAvailable": "100", "item": {"items": [{"line": 1, "custom": {"quantityAvailable": "100"}}]}}
    before = alignment.snapshot(raw)
    changed = deepcopy(raw)
    changed["quantityAvailable"] = "99"
    assert alignment.snapshot(changed)["native_digest"] != before["native_digest"]
    changed = deepcopy(raw)
    changed["item"]["items"][0]["custom"]["quantityAvailable"] = "99"
    assert alignment.snapshot(changed)["native_digest"] != before["native_digest"]


def with_live_availability(projected, available, *, amendable=False):
    raw = {k: deepcopy(v) for k, v in projected.items() if k not in {"lines", "native_digest"}}
    raw["item"] = {"items": [{**deepcopy(line), "quantityAvailable": available} for line in projected["lines"]]}
    return alignment.snapshot(raw, amendable=amendable)


def test_exact_connector_record_fields_and_fresh_evidence_required():
    d = inputs()
    d["review"]["native_mcp_connector_id"] = "00000000-0000-0000-0000-000000000001"
    p = alignment.build_candidate(**d)
    tool = "ext__00000000000000000000000000000001__ns_updateRecord"
    normalized = normalize_write_payload(
        {"recordType": "salesorder", "recordId": "90", "data": json.dumps(p["proposed_fields"])}
    )
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    assert alignment.review_for_card(db, d["tenant_id"], tool, "salesorder", normalized) == p
    with pytest.raises(ValueError):
        alignment.review_for_card(db, "wrong-tenant", tool, "salesorder", normalized)
    p["observed_at"] = "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="Refresh"):
        alignment.review_for_card(db, d["tenant_id"], tool, "salesorder", normalized)


@pytest.mark.asyncio
async def test_conflicting_receipt_is_not_retried():
    p = alignment.build_candidate(**inputs())
    result = await alignment.verify_after(None, p["tenant_id"], p, {"recordId": "99"})
    assert result["status"] == "needs_review" and result["retry_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [None, "availability", "source", "before", "invoice_gl", "invoice_applications", "invoice", "profile", "connector"],
)
async def test_approval_revalidates_frozen_evidence_and_configuration(monkeypatch, changed):
    from uuid import uuid4

    d = inputs()
    d["case_id"] = uuid4()
    d["review"]["native_mcp_connector_id"] = str(uuid4())
    p = alignment.build_candidate(**d)
    fresh = deepcopy(p)
    review = deepcopy(d["review"])
    if changed == "availability":
        p["before"] = with_live_availability(p["before"], "100", amendable=True)
        fresh["before"] = with_live_availability(fresh["before"], "99", amendable=True)
        p["support"]["invoice"] = with_live_availability(p["support"]["invoice"], "100")
        fresh["support"]["invoice"] = with_live_availability(fresh["support"]["invoice"], "99")
    if changed in {"source", "before"}:
        fresh[changed]["total"] = "94"
    elif changed in {"invoice_gl", "invoice_applications", "invoice"}:
        fresh["support"][changed]["unexpected_change"] = True
    if changed == "profile":
        review["sales_credit_profile"]["item_id"] = "99"
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(
            return_value=SimpleNamespace(
                status="active", is_enabled=changed != "connector", server_url="https://123.suitetalk.api.netsuite.com"
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.case_service.get_case",
        AsyncMock(return_value=SimpleNamespace(id=d["case_id"], scope_json={}, latest_report_json={})),
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=d["source"])
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock())
    monkeypatch.setattr(alignment, "prepare", AsyncMock(return_value=fresh))
    call = alignment.validate_approved(
        None,
        d["tenant_id"],
        f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord",
        {"recordType": "salesorder", "recordId": p["record_id"], "data": json.dumps(p["proposed_fields"])},
        p,
    )
    if changed not in {None, "availability"}:
        with pytest.raises(ValueError):
            await call
    else:
        await call


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, "availability", "invoice", "gl", "fulfillment", "custom_field", "source"])
async def test_readback_verifies_order_and_preserves_posting_evidence(monkeypatch, changed):
    from uuid import uuid4

    d = inputs()
    d["case_id"] = uuid4()
    p = alignment.build_candidate(**d)
    fresh = {"order": deepcopy(p["before"]), **deepcopy(p["support"])}
    fresh["order"].update(total="95", discountItem={"id": "50"}, discountRate="-5", discountTotal="-5")
    if changed == "availability":
        p["before"] = with_live_availability(p["before"], "100", amendable=True)
        fresh["order"] = with_live_availability(fresh["order"], "99", amendable=True)
        p["support"]["invoice"] = with_live_availability(p["support"]["invoice"], "100")
        fresh["invoice"] = with_live_availability(fresh["invoice"], "99")
    sections = {
        "gl": {"20": deepcopy(fresh["invoice_gl"])},
        "invoice_applications": fresh["invoice_applications"],
        "invoice_discount_item": fresh["discount_item"],
    }
    source = deepcopy(p["source"])
    if changed == "invoice":
        fresh["invoice"]["lastModifiedDate"] = "changed"
    if changed == "gl":
        sections["gl"]["20"]["rows"][1]["account"] = "999"
    if changed == "fulfillment":
        fresh["order"]["lines"][0]["quantityFulfilled"] = "0"
    if changed == "custom_field":
        fresh["order"]["custbody_example"] = "changed"
    if changed == "source":
        source["total"] = "94"
    monkeypatch.setattr(alignment, "read_support", AsyncMock(return_value=fresh))
    monkeypatch.setattr(
        "app.services.transaction_ops.case_service.get_case",
        AsyncMock(return_value=SimpleNamespace(scope_json={}, latest_report_json={})),
    )
    monkeypatch.setattr("app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence",
        AsyncMock(return_value={"sections": sections, **d["evidence"]}),
    )
    monkeypatch.setattr("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock())
    result = await alignment.verify_after(None, p["tenant_id"], p)
    assert result["status"] == ("verified" if changed in {None, "availability"} else "needs_review")
    assert result["retry_allowed"] is False
