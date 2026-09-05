from app.schemas.transaction_runs import ProposalDecision
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state_api import seed_proposal


async def test_approval_does_not_imply_an_operation_was_attempted(client, db, admin_user):
    actor, headers = admin_user
    _, _, proposal = await seed_proposal(db, actor)
    response = await client.get(f"/api/v1/transaction-ops/proposals/{proposal.id}/operation", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() is None


async def test_execution_state_is_read_from_the_committed_ledger(client, db, admin_user):
    actor, headers = admin_user
    _, _, proposal = await seed_proposal(db, actor)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
        actor=actor,
    )
    await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, fresh_evidence_fingerprint=proposal.evidence_fingerprint
    )
    response = await client.get(f"/api/v1/transaction-ops/proposals/{proposal.id}/operation", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executing"
    assert response.json()["completed_at"] is None


async def test_proposals_can_be_paginated_beyond_the_first_page(client, db, admin_user):
    actor, headers = admin_user
    _, run, _ = await seed_proposal(db, actor)
    first = await client.get(f"/api/v1/transaction-ops/proposals?run_id={run.id}&limit=1&offset=0", headers=headers)
    second = await client.get(f"/api/v1/transaction-ops/proposals?run_id={run.id}&limit=1&offset=1", headers=headers)
    assert first.status_code == second.status_code == 200
    assert len(first.json()) == 1 and second.json() == []
