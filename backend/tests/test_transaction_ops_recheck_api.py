from uuid import UUID, uuid4

from app.services.transaction_ops import recovery, runner
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_executor as execution_fixtures
from tests import test_transaction_ops_recovery as recovery_fixtures

execution_case = execution_fixtures.execution_case
unknown_case = recovery_fixtures.unknown_case


async def test_human_can_recheck_after_inconclusive_recovery_without_resending(
    client, db, admin_user, unknown_case, monkeypatch
):
    actor, headers = admin_user
    case = unknown_case
    row = await execution_fixtures.operation(db, case)
    recovery_fixtures.mock_recovery(monkeypatch, case, unchanged=True)
    assert (await recovery.recover_operation(db, actor.tenant_id, row.id))["status"] == "unknown"
    original_spend = row.api_calls_used
    url = f"/api/v1/transaction-ops/proposals/{case.proposal.id}/recheck"
    body = {"evaluation_key": str(uuid4())}
    first = await client.post(url, json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["origin"] == "recovery" and first.json()["max_api_calls"] == 32
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == first.json()["id"]
    concurrent = await client.post(url, json={"evaluation_key": str(uuid4())}, headers=headers)
    assert concurrent.status_code == 409
    recovery_fixtures.mock_recovery(monkeypatch, case)
    result = await runner.run_investigation(db, actor.tenant_id, UUID(first.json()["id"]))
    assert result["status"] == "verified"
    case.case.dispatch.assert_awaited_once()
    assert (await execution_fixtures.operation(db, case)).api_calls_used == original_spend


async def test_recheck_rejects_actor_forgery_and_missing_proposal(client, db, admin_user, unknown_case):
    actor, headers = admin_user
    url = f"/api/v1/transaction-ops/proposals/{unknown_case.proposal.id}/recheck"
    response = await client.post(url, json={"evaluation_key": str(uuid4()), "actor_id": str(actor.id)}, headers=headers)
    assert response.status_code == 422
    response = await client.post(
        f"/api/v1/transaction-ops/proposals/{uuid4()}/recheck", json={"evaluation_key": str(uuid4())}, headers=headers
    )
    assert response.status_code == 404


async def test_recheck_cannot_read_or_reconcile_another_tenants_operation(client, db, unknown_case):
    from tests.conftest import create_test_tenant, create_test_user, enable_feature_flag, make_auth_headers

    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, tenant.id, flag)
    response = await client.post(
        f"/api/v1/transaction-ops/proposals/{unknown_case.proposal.id}/recheck",
        json={"evaluation_key": str(uuid4())},
        headers=make_auth_headers(user),
    )
    assert response.status_code == 404
    unknown_case.case.dispatch.assert_awaited_once()


async def test_worker_cannot_create_a_human_recheck_without_a_current_actor(db, unknown_case):
    import pytest

    row = await execution_fixtures.operation(db, unknown_case)
    with pytest.raises(state.StateError):
        await state.create_operation_recovery(db, unknown_case.actor.tenant_id, row.id, evaluation_key=uuid4())
    unknown_case.case.dispatch.assert_awaited_once()
