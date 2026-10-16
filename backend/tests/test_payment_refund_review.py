from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from app.models.transaction_ops import TransactionCase, TransactionFinding
from app.services.reconciliation.refund_review import saved_refunds
from app.services.transaction_ops.case_service import case_scope
from tests.test_transaction_period_membership import setup
from tests.test_transaction_review_results import evidence, recheck


async def test_http_bounds_auth_and_cross_tenant_run(app, client, db, admin_user, tenant_b, monkeypatch):
    from app.api.v1.reconciliation import router
    from tests.conftest import create_test_recon_run

    app.include_router(router, prefix="/api/v1")
    actor, headers = admin_user
    await setup(db, admin_user, monkeypatch)
    run = await create_test_recon_run(db, actor.tenant_id)
    other = await create_test_recon_run(db, tenant_b.id)
    url = f"/api/v1/reconciliation/runs/{run.id}/refund-checks"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url + "?limit=101", headers=headers)).status_code == 422
    assert (await client.get(url + "?offset=-1", headers=headers)).status_code == 422
    assert (
        await client.get(f"/api/v1/reconciliation/runs/{other.id}/refund-checks", headers=headers)
    ).status_code == 404
    result = await client.get(url, headers=headers)
    assert result.status_code == 200 and result.json()["provider_reads"] == 0


def refunds(row, source="100.00", target=None):
    report = row.report_json
    row.report_json = {
        **report,
        "refund_evidence": {"source": {"complete": True}, "target": {"complete": target is not None}},
        "balance": {
            **report["balance"],
            "amounts": {**report["balance"]["amounts"], "refunds": {"source": source, "target": target}},
        },
    }


async def test_later_refunds_remain_visible_after_order_closure_and_clear_when_posted(db, admin_user, monkeypatch):
    actor, root, daily, _ = await setup(db, admin_user, monkeypatch)
    rows = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == daily.id))).all()
    for row in rows:
        refunds(row)
        row.report_json = {
            **row.report_json,
            "_observation": {"final": True, "observed_at": "2026-08-03T07:00:00+00:00"},
        }
    case = TransactionCase(
        tenant_id=actor.tenant_id,
        case_key=uuid4().hex,
        order_reference="R123456788",
        scope_json=case_scope(root),
        status="reconciled",
        first_observed_at=root.created_at,
        last_observed_at=root.created_at,
        latest_report_json={"balance": {"status": "matched"}},
    )
    db.add(case)
    await db.flush()
    payment_run = SimpleNamespace(date_from=date(2026, 8, 2), date_to=date(2026, 8, 2), subsidiary_id=None)
    original_count = await db.scalar(select(func.count()).select_from(TransactionFinding))
    for _ in range(2):
        value = await saved_refunds(db, actor.tenant_id, payment_run)
        assert value["provider_reads"] == 0
        assert value["total"] == 1
        item = value["items"][0]
        assert item["order_reference"] == "R123456788"  # Exact day membership, not the wider scan's other order.
        assert item["historical_order_reconciled"] is True
        assert item["status"] == "not_verified" and item["netsuite_amount"] is None
    later = await recheck(db, actor, root)
    row = await evidence(db, actor, later, "R123456788", "matched", daily.created_at + timedelta(seconds=1))
    refunds(row, target="100.00")
    row.report_json = {**row.report_json, "_observation": {"final": True, "observed_at": "2026-08-03T08:00:00+00:00"}}
    row.updated_at = daily.created_at + timedelta(seconds=1)
    await db.flush()
    result = await saved_refunds(db, actor.tenant_id, payment_run)
    assert result["total"] == 0, {"actual": result, "new": str(row.id), "when": str(row.updated_at)}
    assert case.status == "reconciled"
    assert await db.scalar(select(func.count()).select_from(TransactionFinding)) == original_count + 1
    assert (await saved_refunds(db, uuid4(), payment_run))["total"] == 0


async def test_refund_gap_is_exact_and_search_is_literal(db, admin_user, monkeypatch):
    actor, root, daily, _ = await setup(db, admin_user, monkeypatch)
    row = await db.scalar(
        select(TransactionFinding).where(
            TransactionFinding.run_id == daily.id, TransactionFinding.order_reference == "R123456788"
        )
    )
    refunds(row, source="709.00", target="644.00")
    await db.flush()
    scope = SimpleNamespace(date_from=date(2026, 8, 2), date_to=date(2026, 8, 2), subsidiary_id=None)
    item = (await saved_refunds(db, actor.tenant_id, scope))["items"][0]
    assert item["difference"] == "65.00" and item["status"] == "difference"
    assert (await saved_refunds(db, actor.tenant_id, scope, search="%"))["items"] == []
