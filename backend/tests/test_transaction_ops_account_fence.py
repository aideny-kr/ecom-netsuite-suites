from datetime import datetime, timezone

import pytest

from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import action_scheduler
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state_db import new_proposal, seed_config


@pytest.fixture
async def aliases(db, admin_user):
    actor, _ = admin_user
    config = await seed_config(db, actor.tenant_id, actor)
    alternate = config.netsuite_account_id.lower().replace("_", "-")
    assert alternate != config.netsuite_account_id
    other = await seed_config(db, actor.tenant_id, actor, netsuite_account_id=alternate)
    proposals = []
    for n, conf in enumerate((config, other)):
        run = await state.create_run(
            db,
            actor.tenant_id,
            conf.id,
            RunCreate(evaluation_key=f"account-alias-{n}", order_references=["R123456789"]),
            actor=actor,
        )
        proposal = await new_proposal(db, actor, run)
        await state.decide_proposal(
            db,
            actor.tenant_id,
            proposal.id,
            ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
            actor=actor,
        )
        proposals.append(proposal)
    await state.claim_approved_operation(
        db, actor.tenant_id, proposals[0].id, expected_evidence_fingerprint=proposals[0].evidence_fingerprint
    )
    return actor, proposals[1]


async def test_account_spelling_cannot_bypass_an_unsettled_order_fence(db, aliases):
    actor, second = aliases
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await state.claim_approved_operation(
            db, actor.tenant_id, second.id, expected_evidence_fingerprint=second.evidence_fingerprint
        )


async def test_scheduler_does_not_queue_a_second_account_spelling_for_an_unsettled_order(db, aliases):
    actor, second = aliases
    executions, recoveries = await action_scheduler._candidates(db, actor.tenant_id, datetime.now(timezone.utc))
    assert second.id not in executions
    assert recoveries == []
