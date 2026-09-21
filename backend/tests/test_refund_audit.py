from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops.credit_reallocation import build_intent
from app.services.transaction_ops.line_evidence import compare_source_lines, source_revision_delta
from app.services.transaction_ops.refund_audit import audit_query, read_audit, review_allocation
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_credit_reallocation import fixture


def audit_fixture():
    source, review, evidence, support = fixture()
    source["id"] = "100"
    review["solidus_refund_step_id"] = "step"
    link = support["refund_graph"]["request_links"][0]
    link.update(source_refund_id="500", payment_number="PAY100")
    events = [
        {
            "version_id": "1",
            "item_type": "Spree::Order",
            "item_id": "100",
            "event": "update",
            "created_at": "2026-09-08T12:00:00.123456",
            "changes": {
                "total": ["1760", "1320"],
                "item_total": ["1600", "1200"],
                "additional_tax_total": ["160", "120"],
            },
        },
        {
            "version_id": "2",
            "item_type": "Spree::LineItem",
            "item_id": "101",
            "event": "update",
            "created_at": "2026-09-08T12:00:00.123457",
            "previous_order_id": "100",
            "previous_quantity": "1",
            "changes": {"price": ["1600", "1200"]},
        },
        {
            "version_id": "3",
            "item_type": "Spree::LineItem",
            "item_id": "101",
            "event": "update",
            "created_at": "2026-09-08T12:00:00.123458",
            "previous_order_id": "100",
            "previous_quantity": "1",
            "changes": {"additional_tax_total": ["160", "120"], "adjustment_total": ["160", "120"]},
        },
    ]
    for event in events:
        event["changed_keys"] = sorted(event["changes"])
    audit = {
        "source_step_id": "step",
        "connection_id": "connection",
        "row": {
            "order_id": "100",
            "order_reference": source["number"],
            "currency": "USD",
            "refund_id": "500",
            "payment_number": "PAY100",
            "refund_state": 2,
            "refund_reason_id": 38,
            "reimbursement_id": None,
            "processor_reference_present": True,
            "order_refund_count": 1,
            "gross": "440",
            "refund_created_at": "2026-09-08 12:00:00.123459",
            "events": events,
        },
    }
    support["refund_audit"] = audit
    return source, review, evidence, support


def allocation(source, evidence, support):
    return review_allocation(
        support["refund_audit"],
        source,
        support["invoice"],
        source_revision_delta(source, evidence, "20"),
        [c for c in compare_source_lines(source, evidence)["changes"] if c["target_record_id"] == "20"],
        support["refund_graph"]["request_links"][0],
    )


def test_exact_audit_evidence_remains_a_finance_review_candidate():
    source, review, evidence, support = audit_fixture()
    result = allocation(source, evidence, support)
    assert result["status"] == "ready_for_finance_review"
    assert (result["net"], result["tax"], result["gross"]) == ("400", "40", "440")
    assert result["lines"][0]["version_ids"] == ["2", "3"]
    assert "no explicit refund-to-line link" in result["authority"]
    intent = build_intent("tenant", "case", source, review, evidence, support)
    assert intent["refund_allocation"] == result
    assert intent["financial_write_authorized"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_id", "101"),
        ("order_reference", "R999999999"),
        ("currency", "CAD"),
        ("refund_id", "501"),
        ("payment_number", "OTHER"),
        ("refund_state", 1),
        ("refund_state", 3),
        ("refund_reason_id", 1),
        ("reimbursement_id", 99),
        ("processor_reference_present", False),
        ("order_refund_count", 2),
        ("gross", "440.01"),
        ("gross", "NaN"),
        ("gross", True),
    ],
)
def test_identity_and_success_are_required_even_with_matching_timestamps(field, value):
    source, review, evidence, support = audit_fixture()
    support["refund_audit"]["row"][field] = value
    assert allocation(source, evidence, support)["status"] == "needs_review"
    assert build_intent("tenant", "case", source, review, evidence, support) is None


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate_header",
        "duplicate_price",
        "duplicate_version",
        "truncated",
        "quantity",
        "different_owner",
        "future",
        "too_old",
        "header",
        "line_tax",
        "line_price",
        "promotion",
        "included_tax",
        "unexplained_adjustment",
        "destroy",
        "no_price",
        "extra_line",
        "fractional_money",
        "float_money",
    ],
)
def test_ambiguous_incomplete_or_changed_history_never_yields_a_proposal(change):
    source, review, evidence, support = audit_fixture()
    events = support["refund_audit"]["row"]["events"]
    if change == "missing":
        events.clear()
    elif change in ("duplicate_header", "duplicate_price", "duplicate_version"):
        duplicate = deepcopy(events[0 if change == "duplicate_header" else 1])
        if change != "duplicate_version":
            duplicate["version_id"] = "4"
        events.append(duplicate)
    elif change == "truncated":
        events.extend([deepcopy(events[0]) for _ in range(100)])
    elif change == "quantity":
        events[1]["previous_quantity"] = "2"
    elif change == "different_owner":
        events[1]["previous_order_id"] = "200"
    elif change == "future":
        events[1]["created_at"] = "2026-09-08T12:00:00.123460"
    elif change == "too_old":
        events[1]["created_at"] = "2026-09-08T11:54:00"
    elif change == "header":
        events[0]["changes"]["total"] = ["1761", "1321"]
    elif change == "line_tax":
        events[2]["changes"]["additional_tax_total"] = ["161", "121"]
    elif change == "line_price":
        events[1]["changes"]["price"] = ["1601", "1201"]
    elif change == "promotion":
        events[1]["changes"]["promo_total"] = ["0", "-10"]
    elif change == "included_tax":
        events[1]["changes"]["included_tax_total"] = ["4", "3"]
    elif change == "unexplained_adjustment":
        events[2]["changes"]["adjustment_total"] = ["200", "120"]
    elif change == "destroy":
        events[1]["event"] = "destroy"
    elif change == "no_price":
        events.pop(1)
    elif change == "extra_line":
        duplicate = deepcopy(events[1])
        duplicate.update(version_id="4", item_id="102")
        events.append(duplicate)
    elif change == "fractional_money":
        events[1]["changes"]["price"] = ["1600.001", "1200.001"]
    else:
        events[1]["changes"]["price"] = [1600.0, 1200.0]
    assert allocation(source, evidence, support)["status"] == "needs_review"
    assert build_intent("tenant", "case", source, review, evidence, support) is None


def test_configured_audit_cannot_fall_back_to_legacy_evidence_or_another_connector():
    source, review, evidence, support = audit_fixture()
    support["refund_audit"]["source_step_id"] = "another-step"
    assert build_intent("tenant", "case", source, review, evidence, support) is None
    support.pop("refund_audit")
    assert build_intent("tenant", "case", source, review, evidence, support) is None


@pytest.mark.parametrize(
    "reference,refund_id",
    [("R123456789'; DROP TABLE versions", "1"), ("R123456789", "1 OR TRUE"), ("R123456789", True)],
)
def test_query_rejects_interpolation_attacks(reference, refund_id):
    with pytest.raises(SourceReadError, match="invalid_refund_audit_identity"):
        audit_query(reference, refund_id)


async def test_reader_uses_the_existing_tenant_scoped_read_transport(monkeypatch):
    source, _, _, support = audit_fixture()
    row = support["refund_audit"]["row"]
    read = AsyncMock(return_value=([row], SimpleNamespace(id="step"), SimpleNamespace(id="connection")))
    monkeypatch.setattr("app.services.transaction_ops.refund_reader._read_rows", read)
    result = await read_audit("db", "tenant", "step", "R123456789", "500")
    assert read.call_args.args[:3] == ("db", "tenant", "step")
    assert read.call_args.args[4] == 2
    assert "LIMIT 101" in read.call_args.args[3]
    assert result["row"] == row
    read.side_effect = SourceReadError("source_not_found", 404)
    with pytest.raises(SourceReadError, match="source_not_found"):
        await read_audit("db", "other-tenant", "step", "R123456789", "500")


@pytest.mark.parametrize("condition", ["valid", "unavailable", "ambiguous", "missing_refund_id"])
async def test_support_collection_attaches_current_audit_or_explicit_review_blocker(monkeypatch, condition):
    from contextlib import asynccontextmanager

    from app.services.transaction_ops.credit_reallocation import collect_support

    source, review, evidence, support = audit_fixture()
    support["invoice"]["record_type"] = "invoice"
    support["credit"]["record_type"] = "creditmemo"
    support["refund"]["record_type"] = "customerrefund"
    evidence["sections"].update(
        posting_documents=[support["invoice"]],
        related_refund_documents={"documents": [support["credit"], support["refund"]]},
        gl={"20": support["invoice_gl"], "30": support["credit_gl"]},
        taxItem=[support["tax_item"]],
    )
    reader = AsyncMock()
    reader.request.side_effect = [support["currency"], support["item"], support["period"]]

    @asynccontextmanager
    async def factory(*args, **kwargs):
        yield reader

    audit = AsyncMock(return_value=deepcopy(support["refund_audit"]))
    if condition == "unavailable":
        audit.side_effect = SourceReadError("refund_source_unavailable")
    elif condition == "ambiguous":
        audit.return_value["row"]["order_refund_count"] = 2
    elif condition == "missing_refund_id":
        support["refund_graph"]["request_links"][0].pop("source_refund_id")
    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", factory)
    monkeypatch.setattr(
        "app.services.transaction_ops.netsuite_refunds.collect_refunds", AsyncMock(return_value=support["refund_graph"])
    )
    monkeypatch.setattr("app.services.transaction_ops.refund_audit.read_audit", audit)
    actual = await collect_support("db", "tenant", source, review, evidence)
    assert actual["refund_allocation"]["status"] == (
        "ready_for_finance_review" if condition == "valid" else "needs_review"
    )
    if condition == "missing_refund_id":
        audit.assert_not_called()
    else:
        audit.assert_awaited_once_with("db", "tenant", "step", source["number"], "500")
    intent = build_intent("tenant", "case", source, review, evidence, actual)
    assert bool(intent) == (condition == "valid")


@pytest.mark.parametrize(
    "key,values",
    [
        ("full_amount", ["1600", "1200"]),
        ("confirmed_price", ["1600", "1200"]),
        ("payment_state", ["paid", "credit_owed"]),
    ],
)
def test_known_price_mirrors_and_payment_transition_are_validated(key, values):
    source, review, evidence, support = audit_fixture()
    event = support["refund_audit"]["row"]["events"][0 if key == "payment_state" else 1]
    event["changes"][key] = values
    event["changed_keys"].append(key)
    assert allocation(source, evidence, support)["status"] == "ready_for_finance_review"
    event["changes"][key] = ["canceled", "paid"] if key == "payment_state" else ["1601", "1201"]
    assert build_intent("tenant", "case", source, review, evidence, support) is None


@pytest.mark.parametrize("standalone", [False, True])
async def test_sql_preserves_identity_change_keys_without_exposing_their_values(db, standalone):
    """Exercise the actual PostgreSQL projection, not a hand-filtered response."""
    import json
    from datetime import datetime

    from sqlalchemy import text

    definitions = {
        "spree_orders": "id bigint,number text,currency text",
        "spree_payments": "id bigint,order_id bigint,number text",
        "spree_refunds": "id bigint,payment_id bigint,amount numeric,state integer,refund_reason_id bigint,"
        "reimbursement_id bigint,transaction_id text,created_at timestamp",
        "spree_line_items": "id bigint,order_id bigint",
        "versions": "id bigint,item_type text,item_id bigint,event text,created_at timestamp,object jsonb,object_changes jsonb",
    }
    for name, columns in definitions.items():
        await db.execute(text(f"CREATE TEMP TABLE {name} ({columns}) ON COMMIT DROP"))
    await db.execute(text("INSERT INTO spree_orders VALUES (100,'R123456789','USD')"))
    await db.execute(text("INSERT INTO spree_payments VALUES (10,100,'PAY100')"))
    await db.execute(
        text("INSERT INTO spree_refunds VALUES (500,10,440,2,38,NULL,'processor', '2026-09-08 12:00:00.123459')")
    )
    await db.execute(text("INSERT INTO spree_line_items VALUES (101,100)"))
    source, _, evidence, support = audit_fixture()
    source["number"] = evidence["sections"]["sales_order"]["tranId"] = "R123456789"
    events = deepcopy(support["refund_audit"]["row"]["events"])
    if standalone:
        events.append({**deepcopy(events[1]), "version_id": "4", "changes": {"variant_id": [1, 2]}})
    else:
        events[1]["changes"]["variant_id"] = [1, 2]
    for event in events:
        await db.execute(
            text(
                "INSERT INTO versions VALUES (:id,:kind,:item,'update',CAST(:at AS timestamp),"
                "CAST(:object AS jsonb),CAST(:changes AS jsonb))"
            ),
            {
                "id": int(event["version_id"]),
                "kind": event["item_type"],
                "item": int(event["item_id"]),
                "at": datetime.fromisoformat(event["created_at"]),
                "object": json.dumps({"quantity": 1, "order_id": 100}),
                "changes": json.dumps(event["changes"]),
            },
        )
    rows = (await db.execute(text(audit_query("R123456789", "500")))).mappings().all()
    assert len(rows) == 1
    row = dict(rows[0])
    selected = next(e for e in row["events"] if "variant_id" in e["changed_keys"])
    assert "variant_id" not in (selected["changes"] or {})
    support["refund_audit"]["row"] = row
    assert allocation(source, evidence, support)["status"] == "needs_review"


@pytest.mark.parametrize("change", [None, "amount", "quantity", "relationship", "extra_event"])
def test_parent_cache_rollup_is_corroborated_without_double_counting(change):
    source, _, evidence, support = audit_fixture()
    source["line_items"][0]["parent_id"] = "102"
    source["line_items"].append({"id": "102", "price": "0", "quantity": "1", "sku": "KIT", "adjustments": []})
    for doc in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        line = deepcopy(doc["line_evidence"]["lines"][0])
        line.update(
            custcol_fw_solidus_line_id="102", custcol_fw_item_sku="KIT", rate="0", amount="0", custcol_fw_vat_amount="0"
        )
        doc["line_evidence"]["lines"].append(line)
    events = support["refund_audit"]["row"]["events"]
    rollup = {
        **deepcopy(events[1]),
        "version_id": "4",
        "item_id": "102",
        "changes": {"full_amount": ["1600", "1200"]},
        "changed_keys": ["full_amount"],
    }
    events.append(rollup)
    if change == "amount":
        rollup["changes"]["full_amount"] = ["1601", "1201"]
    elif change == "quantity":
        rollup["previous_quantity"] = "2"
    elif change == "relationship":
        source["line_items"][0]["parent_id"] = "999"
    elif change == "extra_event":
        events.append({**deepcopy(rollup), "version_id": "5"})
    result = allocation(source, evidence, support)
    assert result["status"] == ("needs_review" if change else "ready_for_finance_review")
    if change is None:
        assert len(result["lines"]) == 1 and result["gross"] == "440"
        assert result["parent_rollups"] == [
            {"source_line_id": "102", "version_id": "4", "before": "1600", "after": "1200"}
        ]
