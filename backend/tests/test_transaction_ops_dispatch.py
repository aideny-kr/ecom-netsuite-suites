"""A committed operation is not permission to send twice, or after revocation."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_state_db import new_proposal, seed_config


@pytest.fixture
async def ready(db, admin_user):
    actor, _ = admin_user
    config = await seed_config(
        db, actor.tenant_id, actor, mapping_json={"reference_field": "tranid", "action_mode": "propose_actions"}
    )
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="dispatch-test", order_references=["R123456789"]),
        actor=actor,
    )
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
        actor=actor,
    )
    claim = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, fresh_evidence_fingerprint=proposal.evidence_fingerprint
    )
    return actor, config, proposal, claim


async def reserve(db, tenant, claim, **kwargs):
    return await state.reserve_operation_dispatch(
        db, tenant, claim, provider="netsuite", payload_fingerprint="b" * 64, **kwargs
    )


async def test_dispatch_is_committed_and_single_use_even_with_a_new_payload_digest(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim) is True
    operation = (
        await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
    ).scalar_one()
    assert operation.result_json["dispatch_reserved"] is True
    assert operation.result_json["payload_fingerprint"] == "b" * 64
    assert (
        await state.reserve_operation_dispatch(
            db, actor.tenant_id, claim, provider="netsuite", payload_fingerprint="c" * 64
        )
        is False
    )
    assert operation.result_json["payload_fingerprint"] == "b" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"after_json": {"total": "900"}},
        {"operation_id": uuid4()},
        {"proposal_id": uuid4()},
        {"currency": "USD"},
        {"netsuite_account_id": "1234567"},
        {"target_record_id": "999"},
        {"work_key": "c" * 64},
    ],
)
async def test_forged_claim_cannot_consume_a_dispatch(db, ready, change):
    actor, _, _, claim = ready
    with pytest.raises(state.StateError):
        await reserve(db, actor.tenant_id, claim.model_copy(update=change))
    assert await reserve(db, actor.tenant_id, claim) is True


async def test_foreign_tenant_cannot_dispatch(db, ready, admin_user_b):
    _, _, _, claim = ready
    other, _ = admin_user_b
    with pytest.raises(state.StateError):
        await reserve(db, other.tenant_id, claim)


@pytest.mark.parametrize("revoke", ["celigo", "reconciliation", "actor", "permission", "config", "tenant"])
async def test_last_moment_revocation_prevents_dispatch(db, ready, revoke):
    actor, config, _, claim = ready
    if revoke in {"celigo", "reconciliation"}:
        await db.execute(
            text("UPDATE tenant_feature_flags SET enabled=false WHERE tenant_id=:tenant AND flag_key=:flag"),
            {"tenant": actor.tenant_id, "flag": revoke},
        )
    elif revoke == "actor":
        actor.is_active = False
    elif revoke == "permission":
        await db.execute(
            text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:actor"),
            {"tenant": actor.tenant_id, "actor": actor.id},
        )
    elif revoke == "config":
        config.enabled = False
    else:
        await db.execute(text("UPDATE tenants SET is_active=false WHERE id=:tenant"), {"tenant": actor.tenant_id})
    await db.flush()
    with pytest.raises(state.StateError):
        await reserve(db, actor.tenant_id, claim)


async def test_expired_approval_and_wrong_provider_cannot_dispatch(db, ready):
    actor, _, proposal, claim = ready
    with pytest.raises(state.StateError, match="stale_evidence"):
        await reserve(db, actor.tenant_id, claim, now=proposal.valid_until)
    with pytest.raises(state.StateError, match="unsupported_dispatch_provider"):
        await state.reserve_operation_dispatch(
            db, actor.tenant_id, claim, provider="celigo", payload_fingerprint="b" * 64, now=datetime.now(timezone.utc)
        )
