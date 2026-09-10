from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.transaction_ops import TransactionCase, TransactionRun
from app.services.transaction_ops import case_service
from app.services.transaction_ops.state_service import StateError
from tests.test_transaction_cases import NOW, observe, report
from tests.test_transaction_ops_state_db import seed_config


async def cases(db, actor):
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(), NOW)
    first = (await case_service.list_cases(db, actor.tenant_id))[0]
    second = TransactionCase(
        tenant_id=actor.tenant_id,
        case_key=uuid4().hex,
        order_reference="R987654321",
        scope_json=first.scope_json,
        status="open",
        first_observed_at=NOW,
        last_observed_at=NOW,
        latest_report_json={},
    )
    db.add(second)
    await db.flush()
    return first, second


@pytest.mark.asyncio
async def test_bulk_cases_group_by_verified_scope_and_retry_without_duplicate_runs(db, admin_user):
    actor = admin_user[0]
    selected = await cases(db, actor)
    key = uuid4()
    result = await case_service.investigate_cases(db, actor.tenant_id, [c.id for c in selected], key, actor=actor)
    assert len(result["runs"]) == 1 and result["blocked"] == []
    run = await db.get(TransactionRun, result["runs"][0]["id"])
    assert set(run.params_json["order_references"]) == {c.order_reference for c in selected}
    assert run.status == "pending" and run.initiated_by == actor.id
    repeated = await case_service.investigate_cases(db, actor.tenant_id, [c.id for c in selected], key, actor=actor)
    assert repeated["runs"][0]["id"] == run.id


@pytest.mark.asyncio
async def test_bulk_preflights_all_case_ownership_before_any_work_and_rejects_nonhuman(db, admin_user):
    actor = admin_user[0]
    selected = await cases(db, actor)
    before = await db.scalar(select(func.count()).select_from(TransactionRun))
    with pytest.raises(StateError, match="not_found"):
        await case_service.investigate_cases(db, actor.tenant_id, [selected[0].id, uuid4()], uuid4(), actor=actor)
    assert await db.scalar(select(func.count()).select_from(TransactionRun)) == before
    with pytest.raises(StateError):
        await case_service.investigate_cases(db, actor.tenant_id, [selected[0].id], uuid4(), actor=None)


@pytest.fixture(autouse=True)
async def routes(app):
    from app.api.v1.transaction_ops import router

    app.include_router(router, prefix="/api/v1")


async def test_bulk_http_bounds_tenant_permission_and_no_write_decisions(client, db, admin_user):
    from tests.conftest import enable_feature_flag

    actor, headers = admin_user
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    selected = await cases(db, actor)
    url = "/api/v1/transaction-ops/cases/investigate"
    body = {"evaluation_key": str(uuid4()), "case_ids": [str(c.id) for c in selected]}
    assert (await client.post(url, json=body)).status_code == 401
    assert (await client.post(url, headers=headers, json={**body, "case_ids": [str(uuid4())]})).status_code == 404
    assert (
        await client.post(url, headers=headers, json={**body, "case_ids": [str(uuid4()) for _ in range(51)]})
    ).status_code == 422
    result = await client.post(url, headers=headers, json=body)
    assert result.status_code == 202, result.text
    assert len(result.json()["runs"]) == 1
    from app.models.transaction_ops import TransactionOperation, TransactionProposal

    for model in (TransactionProposal, TransactionOperation):
        assert await db.scalar(select(func.count()).select_from(model).where(model.tenant_id == actor.tenant_id)) == 0
