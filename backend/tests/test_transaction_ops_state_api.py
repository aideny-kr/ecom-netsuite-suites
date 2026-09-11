"""Real authenticated API decisions; workers/models never get a decision route."""

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.api.v1.transaction_ops import router
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import scheduler
from app.services.transaction_ops import state_service as state
from tests.conftest import create_test_user, enable_feature_flag, make_auth_headers
from tests.test_transaction_ops_state import proposal_input
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture(autouse=True)
async def transaction_routes(app):
    app.include_router(router, prefix="/api/v1")


@pytest.fixture(autouse=True)
def publisher(monkeypatch):
    publish = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "publish_investigation", publish)
    return publish


async def seed_proposal(db, actor):
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    config = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="api-seed", order_references=["R123456789"]),
        actor=actor,
    )
    lease = await state.claim_run(db, actor.tenant_id, run.id)
    proposal = await state.propose(
        db, actor.tenant_id, run.id, proposal_input(observed_at=datetime.now(timezone.utc)), lease_token=lease
    )
    return config, run, proposal


async def test_trigger_publishes_durable_pending_run(client, db, admin_user, publisher):
    actor, headers = admin_user
    config, _, _ = await seed_proposal(db, actor)
    body = {"evaluation_key": "api-request", "order_references": ["R123456789"]}
    response = await client.post(f"/api/v1/transaction-ops/configs/{config.id}/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    publisher.assert_called_once_with(actor.tenant_id, UUID(response.json()["id"]))
    assert (await client.post(f"/api/v1/transaction-ops/configs/{config.id}/runs", json=body, headers=headers)).json()[
        "id"
    ] == response.json()["id"]


async def test_accounting_profile_api_binds_actor_tenant_and_never_approves(client, db, admin_user, admin_user_b):
    from tests.test_accounting_profiles import setup

    actor, headers = admin_user
    for user in (actor, admin_user_b[0]):
        await enable_feature_flag(db, user.tenant_id, "celigo")
        await enable_feature_flag(db, user.tenant_id, "reconciliation")
    config, _, profile = await setup(db, actor)
    url = f"/api/v1/transaction-ops/configs/{config.id}/accounting-profile"
    body = {"sales_credit_profile": profile}
    assert (await client.put(url, json=body)).status_code == 401
    foreign = await client.put(url, json=body, headers=admin_user_b[1])
    assert foreign.status_code == 404
    assert (await client.put(url, json={**body, "approved_by": str(actor.id)}, headers=headers)).status_code == 422
    saved = await client.put(url, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["financial_writes"] == 0
    assert saved.json()["sales_credit_profile"] == profile
    assert (
        await client.put(url, json={"sales_credit_profile": {"account_id": "999"}}, headers=headers)
    ).status_code == 422
    assert (await client.put(url, json={}, headers=headers)).status_code == 422


async def test_api_actor_cannot_be_supplied_and_pending_decision_is_locked(client, db, admin_user):
    actor, headers = admin_user
    _, _, proposal = await seed_proposal(db, actor)
    url = f"/api/v1/transaction-ops/proposals/{proposal.id}/decision"
    body = {"decision": "approve", "evidence_fingerprint": "a" * 64}
    response = await client.post(url, json={**body, "approved_by": str(actor.id)}, headers=headers)
    assert response.status_code == 422
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["decided_by"] == str(actor.id)
    assert (await client.post(url, json=body, headers=headers)).status_code == 409


async def test_operator_without_recon_approval_permission_cannot_decide(client, db, admin_user, tenant_a):
    actor, _ = admin_user
    _, _, proposal = await seed_proposal(db, actor)
    other, _ = await create_test_user(db, tenant_a, role_name="ops")
    response = await client.post(
        f"/api/v1/transaction-ops/proposals/{proposal.id}/decision",
        json={"decision": "approve", "evidence_fingerprint": "a" * 64},
        headers=make_auth_headers(other),
    )
    assert response.status_code == 403


async def test_disabled_feature_denies_transaction_state(client, db, admin_user):
    actor, headers = admin_user
    _, run, _ = await seed_proposal(db, actor)
    await enable_feature_flag(db, actor.tenant_id, "celigo", False)
    assert (await client.get(f"/api/v1/transaction-ops/runs/{run.id}", headers=headers)).status_code == 403


async def test_reconciliation_flag_off_denies_run_and_approval_api(client, db, admin_user):
    actor, headers = admin_user
    _, run, proposal = await seed_proposal(db, actor)
    await enable_feature_flag(db, actor.tenant_id, "reconciliation", False)
    assert (await client.get(f"/api/v1/transaction-ops/runs/{run.id}", headers=headers)).status_code == 403
    response = await client.post(
        f"/api/v1/transaction-ops/proposals/{proposal.id}/decision",
        headers=headers,
        json={"decision": "approve", "evidence_fingerprint": "a" * 64},
    )
    assert response.status_code == 403
