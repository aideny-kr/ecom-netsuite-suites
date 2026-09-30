from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.transaction_ops import TransactionFinding
from app.schemas.transaction_runs import ReviewSpan, RunCreate
from app.services.transaction_ops import framework_defaults, state_service
from tests.test_transaction_defaults import connections
from tests.test_transaction_tables import seed_orders


async def finding(db, user, order, *, source=None, status="matched", at=None, saved=False):
    source = source or (await connections(db, user.tenant_id))[0]
    config = (await framework_defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user))[0]
    run = await state_service.create_run(
        db,
        user.tenant_id,
        config.id,
        RunCreate(evaluation_key=str(at or status), order_references=(order.order_number,))
        if not saved
        else RunCreate(
            evaluation_key=str(at),
            evidence_mode="saved",
            window_start=at - timedelta(days=1),
            window_end=at,
            review=ReviewSpan(id=uuid4(), start=at - timedelta(days=1), end=at),
        ),
        actor=user,
        now=at if saved else None,
    )
    row = TransactionFinding(
        tenant_id=user.tenant_id,
        run_id=run.id,
        order_reference=order.order_number,
        report_json={
            "source": {"record_id": order.source_id},
            "balance": {
                "status": status,
                "currency": order.currency,
                "amounts": {"refunds": {"source": "0.00", "target": "0.00", "delta": "0.00"}},
            },
            "private_evidence": "not-for-table",
        },
        created_at=at or datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return source, row


async def test_order_table_exposes_scoped_balance_and_filters_before_count_and_export(client, db, admin_user):
    user, headers = admin_user
    first, second = await seed_orders(db, user.tenant_id, ["R100120031", "R100120032"])
    source, evidence = await finding(db, user, first)
    first.source_connection_id = source.id
    await db.flush()
    response = await client.get(
        "/api/v1/tables/orders", headers=headers, params={"reconciliation_status": "matched", "page_size": 1}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    row = response.json()["items"][0]
    assert row["reconciliation"]["run_id"] == str(evidence.run_id)
    assert row["reconciliation"]["status"] == "matched"
    assert "not-for-table" not in response.text
    export = await client.get(
        "/api/v1/tables/orders/export/csv", headers=headers, params={"reconciliation_status": "matched"}
    )
    assert first.order_number in export.text and second.order_number not in export.text


@pytest.mark.parametrize("mismatch", ["connection", "tenant", "identity", "stale", "updated"])
async def test_unrelated_or_stale_evidence_never_marks_order_matched(client, db, admin_user, admin_user_b, mismatch):
    user, headers = admin_user
    order = (await seed_orders(db, user.tenant_id, ["R100120031"]))[0]
    source, evidence = await finding(
        db,
        admin_user_b[0] if mismatch == "tenant" else user,
        order,
        at=datetime.now(timezone.utc) - timedelta(days=2) if mismatch == "stale" else None,
    )
    if mismatch != "connection":
        order.source_connection_id = source.id
    if mismatch == "identity":
        order.source_id = "different-record"
    if mismatch == "updated":
        order.source_updated_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    await db.flush()
    response = await client.get(
        "/api/v1/tables/orders", headers=headers, params={"reconciliation_status": "not_verified"}
    )
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["reconciliation"]["status"] == "not_verified"


async def test_invalid_reconciliation_filter_is_not_silently_ignored(client, admin_user):
    for table, status in [("orders", "whatever"), ("payments", "matched")]:
        response = await client.get(
            f"/api/v1/tables/{table}", headers=admin_user[1], params={"reconciliation_status": status}
        )
        assert response.status_code == 422


async def test_source_connection_filter_applies_to_order_rows_and_exports(client, db, admin_user):
    user, headers = admin_user
    first, second = await seed_orders(db, user.tenant_id, ["R100120031", "R100120032"])
    source, _ = await connections(db, user.tenant_id)
    first.source_connection_id = source.id
    await db.flush()
    params = {"source_connection_id": str(source.id)}
    response = await client.get("/api/v1/tables/orders", headers=headers, params=params)
    assert response.json()["total"] == 1
    export = await client.get("/api/v1/tables/orders/export/csv", headers=headers, params=params)
    assert first.order_number in export.text and second.order_number not in export.text


@pytest.mark.parametrize("stale_by", ["age", "source_update"])
async def test_saved_copy_never_refreshes_order_verification(client, db, admin_user, stale_by):
    from copy import deepcopy

    user, headers = admin_user
    order = (await seed_orders(db, user.tenant_id, ["R100120031"]))[0]
    observed = datetime.now(timezone.utc) - timedelta(days=2 if stale_by == "age" else 0, hours=1)
    source, original = await finding(db, user, order, at=observed)
    order.source_connection_id = source.id
    if stale_by == "source_update":
        order.source_updated_at = observed + timedelta(minutes=1)
    _, copied = await finding(db, user, order, source=source, at=datetime.now(timezone.utc), saved=True)
    copied.report_json = {**deepcopy(original.report_json), "cached_evidence": {"basis": "historical_as_observed"}}
    await db.flush()
    response = await client.get(
        "/api/v1/tables/orders", headers=headers, params={"reconciliation_status": "not_verified"}
    )
    assert response.status_code == 200 and response.json()["total"] == 1
    evidence = response.json()["items"][0]["reconciliation"]
    assert evidence["finding_id"] == str(original.id)
    assert evidence["stale"] and evidence["status"] == "not_verified"
    assert datetime.fromisoformat(evidence["checked_at"]) == observed
