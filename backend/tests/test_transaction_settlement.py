"""A verified write must lead to bounded, separately proven financial settlement."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import run_investigation
from tests.test_transaction_balance_report import evidence
from tests.test_transaction_ops_state_db import new_proposal, seed_config


@pytest.fixture
async def settled_operation(db, admin_user, request):
    actor = admin_user[0]
    source, target, _, mapping, _ = evidence()
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        netsuite_account_id="6738075",
        subsidiary_id="1",
        mapping_json={
            **mapping.model_dump(mode="json"),
            "action_mode": "propose_actions",
            "solidus_refund_step_id": str(uuid4()),
        },
    )
    ref = source["orders"][0]["number"]
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="before-fix", order_references=[ref]), actor=actor
    )
    proposal = await new_proposal(db, actor, run, order_reference=ref, currency="USD", target_record_id="200")
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=run.lease_token)
    claim = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64
    )
    operation = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome=getattr(request, "param", "verified"),
        result_json={"code": "independently_verified"},
    )
    return actor, config, proposal, operation, source, target


async def settlement_run(db, actor, operation):
    rows = list(
        await db.scalars(
            select(TransactionRun).where(
                TransactionRun.tenant_id == actor.tenant_id,
                TransactionRun.params_json["operation_id"].astext == str(operation.id),
                TransactionRun.params_json["verification_scope"].astext == "order_total_tax_refunds",
            )
        )
    )
    assert len(rows) == 1
    return rows[0]


async def test_verified_operation_atomically_queues_one_scoped_read_only_check(db, settled_operation, tenant_b):
    actor, config, proposal, operation, _, _ = settled_operation
    run = await settlement_run(db, actor, operation)
    assert run.status == "pending" and run.origin == "recovery"
    assert run.config_id == config.id
    assert run.params_json["order_references"] == [proposal.order_reference]
    assert run.max_orders == 1 and run.max_api_calls <= 64
    assert run.initiated_by is None
    with pytest.raises(state.StateError, match="operation_terminal"):
        await state.complete_operation(db, actor.tenant_id, operation.id, outcome="verified", result_json={})
    assert (await settlement_run(db, actor, operation)).id == run.id
    with pytest.raises(state.StateError, match="not_found"):
        await state.get_run(db, tenant_b.id, run.id)
    with pytest.raises(ValidationError):
        RunCreate(
            origin="recovery",
            evaluation_key="forged",
            order_references=[proposal.order_reference],
            operation_id=operation.id,
            verification_scope="order_total_tax_refunds",
        )


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("match", "succeeded"),
        ("penny", "difference"),
        ("refund_missing", "unverified"),
        ("stale", "unverified"),
        ("wrong_target", "unverified"),
    ],
)
async def test_settlement_uses_complete_fresh_reads_and_never_proposes_or_writes(
    db,
    settled_operation,
    variant,
    expected,
):
    actor, _, proposal, operation, source, target = settled_operation
    run = await settlement_run(db, actor, operation)
    now = datetime.now(timezone.utc)
    observed = now if variant != "stale" else operation.completed_at - timedelta(seconds=1)
    source["read_at"] = target["observed_at"] = observed.isoformat()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.01" if variant == "penny" else "20.00")
    if variant == "wrong_target":
        target["orders"][0]["record_id"] = "201"
        target["orders"][0]["header"]["id"] = "201"
    refund = {"complete": True, "amount": "0.00", "currency": "USD", "order_reference": proposal.order_reference}
    guard = AsyncMock(side_effect=AssertionError("Settlement must not plan another write"))
    result = await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _clock=lambda: now,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=refund),
        _target_refunds_reader=AsyncMock(return_value={"complete": False} if variant == "refund_missing" else refund),
        _guard_reader=guard,
        _celigo_reader=guard,
        _create_reader=guard,
    )
    assert result["termination_reason"] == "done"
    run = await state.get_run(db, actor.tenant_id, run.id)
    assert run.progress_json["settlement"]["status"] == expected
    assert run.progress_json["settlement"]["operation_id"] == str(operation.id)
    assert run.progress_json["settlement"]["approved_by"] == str(actor.id)
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    guard.assert_not_awaited()
    events = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id == str(run.id),
                AuditEvent.action == "transaction_ops.settlement.complete",
            )
        )
    )
    assert len(events) == 1 and events[0].payload["status"] == expected
    before = run.progress_json
    await run_investigation(db, actor.tenant_id, run.id)
    assert (await state.get_run(db, actor.tenant_id, run.id)).progress_json == before


async def test_queued_settlement_gets_execution_time_and_cannot_create_proposals(db, settled_operation):
    actor, _, _, operation, _, _ = settled_operation
    run = await settlement_run(db, actor, operation)
    later = datetime.now(timezone.utc)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=later)
    assert token is not None
    from tests.test_transaction_ops_state import proposal_input

    with pytest.raises(state.StateError, match="read_only_verification_run"):
        await state.propose(
            db, actor.tenant_id, run.id, proposal_input(observed_at=later), lease_token=token, now=later
        )
    await state.finish_run(db, actor.tenant_id, run.id, "error", lease_token=token, now=later)
    assert run.progress_json["settlement"]["status"] == "unverified"


@pytest.mark.parametrize("settled_operation", ["unknown", "failed"], indirect=True)
async def test_unknown_or_failed_write_is_not_queued_as_verified_settlement(db, settled_operation):
    from app.services.transaction_ops import settlement

    actor, _, _, operation, _, _ = settled_operation
    result = await settlement.status(db, actor.tenant_id, operation.id)
    assert result["status"] == "not_evaluated" and result["run_id"] is None


async def test_settlement_api_requires_auth_and_operation_tenant(
    client, app, db, settled_operation, admin_user, tenant_b
):
    from app.api.v1.transaction_ops import router
    from app.services.transaction_ops import settlement
    from tests.conftest import enable_feature_flag

    app.include_router(router, prefix="/api/v1")
    actor, _, _, operation, _, _ = settled_operation
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    url = f"/api/v1/transaction-ops/operations/{operation.id}/settlement"
    assert (await client.get(url)).status_code == 401
    response = await client.get(url, headers=admin_user[1])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["verification_scope"] == "order_total_tax_refunds"
    with pytest.raises(state.StateError, match="not_found"):
        await settlement.status(db, tenant_b.id, operation.id)
